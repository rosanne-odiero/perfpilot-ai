"""End-to-end Runtime Audit against the REAL Docker sandbox (``@pytest.mark.docker``).

Skipped with the reason unless a Docker daemon is reachable and ``perfpilot-sandbox:latest`` exists
(``make sandbox-image``). No test double is installed here: the worker builds the real
``DockerSandboxBackend``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from audit_support import SHOPFAST
from fastapi.testclient import TestClient
from runtime_support import WORKLOAD_ARGV, approve, command_by_argv, drain

from backend.core.config import Settings
from backend.models.database import session_scope
from backend.models.enums import ReportFormat
from backend.reporting.builder import build_audit_report
from backend.reporting.renderers import render_report
from backend.runtime.backends import set_backend_factory
from backend.sandbox.docker_backend import CONTAINER_LABEL, find_docker_executable

pytestmark = pytest.mark.docker

PYTEST_ARGV = ["python", "-m", "pytest", "-q"]


@pytest.fixture
def repos_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PERFPILOT_LOCAL_ALLOWLIST", str(root))
    set_backend_factory(None)  # the real Docker backend, never a test double
    return root


@pytest.fixture
def api(repos_dir: Path, client: TestClient) -> TestClient:
    return client


def _import_shopfast(api: TestClient, settings: Settings, repos_dir: Path, **extra: str) -> str:
    source = repos_dir / "shopfast"
    shutil.copytree(SHOPFAST, source, ignore=shutil.ignore_patterns("__pycache__"))
    for rel, text in extra.items():
        (source / rel).write_text(text)
    response = api.post(
        "/api/repositories", json={"source_type": "LOCAL_DIRECTORY", "local_path": str(source)}
    )
    assert response.status_code == 202, response.text
    drain(settings)
    snapshot_id = str(response.json()["subject_id"])
    assert api.get(f"/api/snapshots/{snapshot_id}").json()["status"] == "READY"
    return snapshot_id


def _runtime_audit(api: TestClient, settings: Settings, snapshot_id: str) -> str:
    response = api.post("/api/audits", json={"snapshot_id": snapshot_id, "mode": "RUNTIME"})
    assert response.status_code == 202, response.text
    drain(settings)
    audit_id = str(response.json()["subject_id"])
    audit = api.get(f"/api/audits/{audit_id}").json()
    assert audit["stage"] == "AWAITING_RUNTIME_APPROVAL"
    assert audit["summary"]["runtime"]["runtime_available"] is True, audit["warnings"]
    return audit_id


def _profile(
    api: TestClient, settings: Settings, audit_id: str, argv: list[str], **extra: Any
) -> dict[str, Any]:
    accepted = approve(api, audit_id, command_by_argv(api, audit_id, argv), **extra)
    drain(settings)
    run: dict[str, Any] = api.get(
        f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}"
    ).json()
    return run


def _leftover_containers() -> list[str]:
    docker = find_docker_executable(Settings())
    assert docker is not None
    listed = subprocess.run(  # fixed argv, no shell
        [str(docker), "ps", "-aq", "--filter", f"label={CONTAINER_LABEL}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return listed.stdout.split()


def test_workload_is_profiled_in_the_real_sandbox_and_correlated(
    api: TestClient, settings: Settings, repos_dir: Path
) -> None:
    audit_id = _runtime_audit(api, settings, _import_shopfast(api, settings, repos_dir))
    assert api.get("/api/runtime/status").json()["available"] is True
    before = set(_leftover_containers())

    run = _profile(api, settings, audit_id, WORKLOAD_ARGV)

    assert run["status"] == "SUCCEEDED", (run["failure_reason"], run["stderr_tail"])
    assert run["provenance"] == "MEASURED"
    env = run["environment"]
    assert env["backend"] == "docker"
    assert env["image_digest"].startswith("sha256:")
    assert env["python_version"].startswith("3.12")
    assert "Linux" in env["platform"]
    assert env["network"] == "none"
    assert run["memory"]["peak_rss_bytes"] > run["memory"]["tracemalloc_peak_bytes"] > 0
    hot = {(h["file_path"], h["function_name"]): h for h in run["hotspots"]}
    reconcile = hot[("shopfast/inventory_sync.py", "reconcile")]
    assert reconcile["rank"] == 1
    assert reconcile["line_number"] == 8
    assert reconcile["symbol"] == "reconcile"
    assert any(h["file_path"].startswith("<stdlib>/") for h in run["hotspots"])
    assert not any(h["file_path"].startswith(("/workspace", "/usr")) for h in run["hotspots"])

    correlated = {(c["rule_id"], c["location"]["symbol"]) for c in run["correlations"]}
    assert ("PP-LOOP-002", "reconcile") in correlated
    assert ("PP-DB-001", "order_summaries") in correlated
    assert ("PP-COLL-001", "duplicate_skus") in correlated
    findings = api.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()["items"]
    top = findings[0]
    assert (top["rule_id"], top["provenance"]) == ("PP-LOOP-002", "STATIC")
    assert top["measured"]["provenance"] == "MEASURED"
    assert top["measured"]["profile_id"] == run["id"]

    with session_scope() as session:
        report = build_audit_report(session, audit_id)
    assert report.runtime is not None
    assert report.runtime.profiles[0].environment["image_digest"] == env["image_digest"]
    page = render_report(report, ReportFormat.HTML).primary.data.decode()
    section = page.split('id="s7"')[1].split('id="s8"')[0]
    assert 'class="badge prov prov-measured"' in section
    assert "reconcile" in section
    assert set(_leftover_containers()) <= before  # every sandbox container was removed


def test_repository_tests_run_in_the_real_sandbox(
    api: TestClient, settings: Settings, repos_dir: Path
) -> None:
    audit_id = _runtime_audit(api, settings, _import_shopfast(api, settings, repos_dir))
    run = _profile(api, settings, audit_id, PYTEST_ARGV)
    assert run["status"] == "SUCCEEDED", (run["failure_reason"], run["stdout_tail"])
    assert "24 passed" in run["stdout_tail"]
    assert any(h["file_path"].startswith("tests/") for h in run["hotspots"] if h["is_user_code"])
    assert any(h["file_path"].startswith("<site-packages>/") for h in run["hotspots"])


def test_failures_and_timeouts_are_recorded_honestly_in_the_real_sandbox(
    api: TestClient, settings: Settings, repos_dir: Path
) -> None:
    snapshot = _import_shopfast(
        api, settings, repos_dir, **{"tests/test_zz_fail.py": "def test_x():\n    assert False\n"}
    )
    audit_id = _runtime_audit(api, settings, snapshot)

    failed = _profile(api, settings, audit_id, PYTEST_ARGV)
    assert failed["status"] == "FAILED"
    assert failed["command_exit_code"] == 1
    assert failed["hotspots"] == []

    timed_out = _profile(
        api, settings, audit_id, WORKLOAD_ARGV, limits_override={"timeout_seconds": 1}
    )
    assert timed_out["status"] == "TIMED_OUT"
    assert timed_out["timed_out"] is True
    assert timed_out["hotspots"] == []
    findings = api.get(f"/api/audits/{audit_id}/findings", params={"limit": 500}).json()["items"]
    assert all(f["measured"] is None for f in findings)
