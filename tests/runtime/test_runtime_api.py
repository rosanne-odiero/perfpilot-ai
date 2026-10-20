"""RUNTIME audit flow through the API: discovery, status, approvals and pinning.

Sandbox execution is simulated by test doubles from ``runtime_support`` (see its docstring).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from audit_support import SHOPFAST
from fastapi.testclient import TestClient
from runtime_support import (
    WORKLOAD_ARGV,
    BackendSetter,
    SubprocessHarnessBackend,
    UnavailableBackend,
    approve,
    command_by_argv,
    drain,
    shopfast_snapshot,
    start_runtime_audit,
)
from sqlalchemy import select

from backend.core.config import Settings
from backend.core.exceptions import ConflictError
from backend.models.database import session_scope
from backend.models.orm.platform import Job
from backend.models.orm.runtime import RuntimeApproval
from backend.models.orm.runtime_profiles import RuntimeProfileRun
from backend.sandbox.approvals import consume_approval
from backend.sandbox.docker_backend import DockerSandboxBackend

EXPECTED = json.loads((SHOPFAST / "EXPECTED_FINDINGS.json").read_text())["findings"]


def _error(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


def test_status_before_any_worker_check_is_unavailable(client: TestClient) -> None:
    status = client.get("/api/runtime/status").json()
    assert status["available"] is False
    assert "no worker has checked" in status["reason"]
    assert status["checked_at"] is None
    assert status["default_limits"]["network"] == "none"
    assert status["image"] == "perfpilot-sandbox:latest"


def test_runtime_audit_discovers_commands_and_awaits_approval(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))

    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["mode"] == "RUNTIME"
    assert audit["status"] == "SUCCEEDED"
    assert audit["stage"] == "AWAITING_RUNTIME_APPROVAL"
    runtime = audit["summary"]["runtime"]
    assert runtime["state"] == "awaiting_approval"
    assert runtime["runtime_available"] is True
    assert runtime["commands_discovered"] >= 2
    assert audit["summary"]["findings_total"] == len(EXPECTED)
    assert harness_backend.runs == []  # discovery never executes anything

    commands = client.get(f"/api/audits/{audit_id}/runtime/commands").json()
    argvs = [c["argv"] for c in commands]
    assert WORKLOAD_ARGV in argvs
    assert ["python", "-m", "pytest", "-q"] in argvs
    workload = command_by_argv(client, audit_id, WORKLOAD_ARGV)
    assert workload["kind"] == "script"
    assert workload["runnable"] is True
    assert workload["provenance"] == "STATIC"
    assert any(e.startswith("benchmarks/run_workload.py:") for e in workload["evidence"])

    status = client.get("/api/runtime/status").json()
    assert status["available"] is True
    assert status["backend"] == "test-double-subprocess"
    assert status["checked_at"] is not None
    assert "docker_path" not in status["details"]
    assert "/host/only/path" not in json.dumps(status)

    overview = client.get(f"/api/audits/{audit_id}/runtime").json()
    assert overview["commands_discovered"] == len(commands)
    assert overview["runtime_requested"] is True
    assert overview["runtime_available"] is True
    assert overview["runs"] == []
    assert overview["measured_run_id"] is None
    assert overview["hotspots"] == []


def test_unavailable_backend_keeps_static_audit_and_rejects_approval(
    client: TestClient, settings: Settings, use_backend: BackendSetter
) -> None:
    use_backend(UnavailableBackend(reason="docker CLI not found (test double)"))
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))

    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    assert audit["stage"] == "AWAITING_RUNTIME_APPROVAL"
    assert audit["summary"]["runtime"]["runtime_available"] is False
    assert "docker CLI not found" in audit["summary"]["runtime"]["unavailable_reason"]
    assert any("Runtime profiling is unavailable" in w for w in audit["warnings"])
    page = client.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()
    assert page["total"] == len(EXPECTED)
    assert all(f["provenance"] == "STATIC" and f["measured"] is None for f in page["items"])
    assert client.get(f"/api/audits/{audit_id}/runtime/commands").json()  # discovery still ran

    status = client.get("/api/runtime/status").json()
    assert status["available"] is False
    assert "docker CLI not found" in status["reason"]

    command = command_by_argv(client, audit_id, WORKLOAD_ARGV)
    response = client.post(
        f"/api/audits/{audit_id}/runtime/approvals",
        json={"command_id": command["id"], "argv": command["argv"], "approved_by": "tester"},
    )
    assert response.status_code == 503
    error = _error(response)
    assert error["code"] == "unavailable"
    assert "docker CLI not found" in error["message"]
    overview = client.get(f"/api/audits/{audit_id}/runtime").json()
    assert overview["runtime_available"] is False
    assert overview["runs"] == []


def test_status_refresh_is_a_worker_job(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    response = client.post("/api/runtime/status/refresh")
    assert response.status_code == 202
    assert client.get("/api/runtime/status").json()["checked_at"] is None
    drain(settings)
    job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
    assert job["status"] == "SUCCEEDED"
    assert job["result"]["available"] is True
    assert "docker_path" not in job["result"]["details"]
    assert client.get("/api/runtime/status").json()["available"] is True


def test_approval_pins_the_exact_discovered_argv(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    command = command_by_argv(client, audit_id, WORKLOAD_ARGV)
    url = f"/api/audits/{audit_id}/runtime/approvals"

    def post(**overrides: Any) -> Any:
        body = {"command_id": command["id"], "argv": command["argv"], "approved_by": "tester"}
        return client.post(url, json={**body, **overrides})

    injected = post(argv=[*WORKLOAD_ARGV, "--scale", "50"])
    assert injected.status_code == 400
    assert _error(injected)["code"] == "security_violation"
    assert post(argv=["python", "-c", "print(1)"]).status_code == 400
    assert post(argv=list(reversed(WORKLOAD_ARGV))).status_code == 400
    assert post(command_id="not-a-command").status_code == 404
    looser = post(limits_override={"memory_mb": settings.sandbox_memory_mb * 4})
    assert looser.status_code == 422
    assert _error(looser)["details"]["looser_than_default"] == ["memory_mb"]
    assert post(approved_by="  ").status_code == 422
    assert post(surprise=True).status_code == 422
    assert (
        client.post(
            "/api/audits/missing/runtime/approvals",
            json={"command_id": command["id"], "argv": command["argv"], "approved_by": "x"},
        ).status_code
        == 404
    )
    with session_scope() as session:
        assert session.scalars(select(RuntimeApproval)).all() == []

    accepted = post(limits_override={"timeout_seconds": 120, "memory_mb": 1024})
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["subject_type"] == "runtime_run"
    with session_scope() as session:
        run = session.get(RuntimeProfileRun, body["subject_id"])
        assert run is not None
        approval = session.get(RuntimeApproval, run.approval_id)
        assert approval is not None
        assert approval.argv == WORKLOAD_ARGV
        assert approval.approved_by == "tester"
        assert approval.limits["timeout_seconds"] == 120
        assert approval.limits["memory_mb"] == 1024
        assert approval.image == settings.sandbox_image
        job = session.get(Job, body["job_id"])
        assert job is not None
        assert job.job_type == "runtime.profile"
        assert job.max_attempts == 1
        assert job.payload == {"run_id": run.id}

    # One run at a time per audit.
    busy = post()
    assert busy.status_code == 409
    assert _error(busy)["details"]["run_id"] == body["subject_id"]


def test_static_audits_cannot_approve_runtime_commands(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    runtime_audit = start_runtime_audit(client, settings, shopfast_snapshot(settings, "rt"))
    command = command_by_argv(client, runtime_audit, WORKLOAD_ARGV)
    static = client.post("/api/audits", json={"snapshot_id": shopfast_snapshot(settings, "st")})
    static_id = static.json()["subject_id"]
    drain(settings)
    response = client.post(
        f"/api/audits/{static_id}/runtime/approvals",
        json={"command_id": command["id"], "argv": command["argv"], "approved_by": "tester"},
    )
    assert response.status_code == 409
    assert client.get(f"/api/audits/{static_id}/runtime/commands").json() == []
    overview = client.get(f"/api/audits/{static_id}/runtime").json()
    assert overview["runtime_requested"] is False
    assert overview["runs"] == []


def test_approval_is_single_use(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    command = command_by_argv(client, audit_id, WORKLOAD_ARGV)
    accepted = approve(client, audit_id, command, trace_memory=False)
    with session_scope() as session:
        run = session.get(RuntimeProfileRun, accepted["subject_id"])
        assert run is not None
        approval_id = run.approval_id
        consume_approval(session, approval_id)  # someone else used it first
    drain(settings)

    assert harness_backend.runs == []
    job = client.get(f"/api/jobs/{accepted['job_id']}").json()
    assert job["status"] == "FAILED"
    assert "already been used" in job["error"]
    detail = client.get(f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}").json()
    assert detail["status"] == "FAILED"
    assert "already been used" in detail["failure_reason"]
    assert detail["hotspots"] == []
    with session_scope() as session, pytest.raises(ConflictError, match="already been used"):
        consume_approval(session, approval_id)


def test_api_process_never_uses_the_docker_backend(
    client: TestClient,
    settings: Settings,
    harness_backend: SubprocessHarnessBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the API process must never use DockerSandboxBackend")

    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    for name in ("__init__", "run", "availability", "install_dependencies"):
        monkeypatch.setattr(DockerSandboxBackend, name, forbidden)

    assert client.get("/api/runtime/status").status_code == 200
    assert client.post("/api/runtime/status/refresh").status_code == 202
    command = command_by_argv(client, audit_id, WORKLOAD_ARGV)
    accepted = approve(client, audit_id, command)
    assert client.get(f"/api/audits/{audit_id}/runtime").status_code == 200
    run_url = f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}"
    assert client.get(run_url).json()["status"] == "QUEUED"
    assert client.get(f"/api/audits/{audit_id}/runtime/commands").status_code == 200
    assert harness_backend.runs == []  # nothing ran until the worker picked up the job

    drain(settings)  # the worker (here: the test double) executes the approved run
    assert len(harness_backend.runs) == 1
    assert client.get(run_url).json()["status"] == "SUCCEEDED"


def test_unknown_audit_and_run_are_404(client: TestClient, settings: Settings) -> None:
    for path in ("/runtime", "/runtime/commands", "/runtime/runs/x"):
        assert client.get(f"/api/audits/missing{path}").status_code == 404


def test_commands_of_another_audit_cannot_be_approved(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    first = start_runtime_audit(client, settings, shopfast_snapshot(settings, "a"))
    second = start_runtime_audit(client, settings, shopfast_snapshot(settings, "b"))
    foreign = command_by_argv(client, first, WORKLOAD_ARGV)
    response = client.post(
        f"/api/audits/{second}/runtime/approvals",
        json={"command_id": foreign["id"], "argv": foreign["argv"], "approved_by": "tester"},
    )
    assert response.status_code == 404
