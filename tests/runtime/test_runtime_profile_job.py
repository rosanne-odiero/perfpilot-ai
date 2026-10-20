"""``runtime.profile`` job: real harness output on ShopFast via the subprocess test double.

``SubprocessHarnessBackend`` (see ``runtime_support``) simulates only the container boundary; the
profile data asserted here is real cProfile/tracemalloc output from running the approved command.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from runtime_support import (
    WORKLOAD_ARGV,
    BackendSetter,
    ScriptedBackend,
    SubprocessHarnessBackend,
    UnavailableBackend,
    approve,
    command_by_argv,
    drain,
    shopfast_snapshot,
    start_runtime_audit,
)

from backend.core.config import Settings
from backend.infrastructure.artifact_store import ArtifactStore
from backend.models.database import session_scope
from backend.models.orm.platform import Artifact
from backend.models.orm.runtime import RuntimeApproval
from backend.profiling.runner import HARNESS_PATH
from backend.runtime.execution import choose_requirements_source, harness_command, outcome_of
from backend.sandbox.backend import SandboxRunRequest, SandboxRunResult
from backend.sandbox.docker_backend import DockerSandboxBackend

FAILING_TEST = "def test_always_fails():\n    assert 1 == 2\n"


def _run(
    client: TestClient, settings: Settings, audit_id: str, argv: list[str], **extra: Any
) -> dict[str, Any]:
    accepted = approve(client, audit_id, command_by_argv(client, audit_id, argv), **extra)
    drain(settings)
    detail: dict[str, Any] = client.get(
        f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}"
    ).json()
    detail["_job"] = client.get(f"/api/jobs/{accepted['job_id']}").json()
    return detail


def _findings(client: TestClient, audit_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = client.get(
        f"/api/audits/{audit_id}/findings", params={"limit": 500}
    ).json()["items"]
    return items


def test_approved_workload_is_profiled_and_correlated(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    static = {f["id"]: f for f in _findings(client, audit_id)}
    run = _run(client, settings, audit_id, WORKLOAD_ARGV)

    # What the worker asked the sandbox to do.
    [request] = harness_backend.runs
    assert request.argv == [
        "python",
        "-P",
        "-s",
        "-B",
        "/perfpilot/harness_entry.py",
        "/perfpilot/request.json",
        "/out/result.json",
    ]
    assert request.network == "none"
    assert request.workdir_rel == "."
    assert request.env == {"PYTHONHASHSEED": "0"}
    assert request.deps_dir is None
    mounts = {m.target: m.source for m in request.extra_mounts}
    assert mounts["/perfpilot/harness_entry.py"] == HARNESS_PATH
    assert harness_backend.harness_requests == [
        {
            "mode": "command",
            "workspace_root": "/workspace",
            "output_dir": "/out",
            "command": {
                "kind": "script",
                "target": "benchmarks/run_workload.py",
                "args": [],
                "trace_memory": True,
                "stats_json": True,
            },
        }
    ]
    assert not mounts["/perfpilot/request.json"].exists()  # per-run scratch is removed

    # The run record: honest status, metrics, environment, artifacts.
    assert run["_job"]["status"] == "SUCCEEDED"
    assert run["status"] == "SUCCEEDED"
    assert run["provenance"] == "MEASURED"
    assert run["exit_code"] == 0
    assert run["command_exit_code"] == 0
    assert run["timed_out"] is False
    assert run["oom_killed"] is False
    assert run["wall_time_ms"] > 0
    assert run["cpu_time_ms"] > 0
    assert run["function_count"] > 100
    memory = run["memory"]
    assert memory["peak_rss_bytes"] > memory["tracemalloc_peak_bytes"] > 0
    env = run["environment"]
    assert env["network"] == "none"
    assert env["python_version"].startswith("3.")
    assert env["limits"]["memory_mb"] == settings.sandbox_memory_mb
    assert env["profiler"] == "cProfile + tracemalloc (concurrent)"
    assert '"checksum"' in run["stdout_tail"]
    with session_scope() as session:
        stored = {
            a.id: a for a in session.query(Artifact).filter(Artifact.owner_id == run["id"]).all()
        }
        assert set(stored) == {v for v in run["artifacts"].values() if v}
        assert len(stored) == 3
        result = json.loads(
            ArtifactStore(settings).read_bytes(stored[run["artifacts"]["harness_result_json"]])
        )
        assert result["status"] == "ok"
        approval = session.get(RuntimeApproval, run["approval_id"])
        assert approval is not None
        assert approval.status == "CONSUMED"

    # Hotspots: real cProfile data mapped to repository paths and indexed symbols.
    hot = {(h["file_path"], h["function_name"]): h for h in run["hotspots"]}
    reconcile = hot[("shopfast/inventory_sync.py", "reconcile")]
    assert reconcile["provenance"] == "MEASURED"
    assert reconcile["line_number"] == 8
    assert reconcile["symbol"] == "reconcile"
    assert reconcile["is_user_code"] is True
    assert reconcile["total_calls"] == 1
    package = [h for h in run["hotspots"] if h["file_path"].startswith("shopfast/")]
    assert max(package, key=lambda h: h["internal_time_ms"]) == reconcile  # O(n*m) nested loop
    assert reconcile["percentage_of_profiled_time"] > 15
    assert not any(h["file_path"].startswith("/") for h in run["hotspots"])
    assert any(h["location_kind"] == "builtin" for h in run["hotspots"])

    # Correlation: findings in measured functions carry MEASURED evidence, stay STATIC themselves.
    findings = _findings(client, audit_id)
    by_key = {
        (f["rule_id"], f["location"]["file_path"], f["location"]["line_start"]): f for f in findings
    }
    nested = by_key[("PP-LOOP-002", "shopfast/inventory_sync.py", 11)]
    assert nested["provenance"] == "STATIC"
    evidence = nested["measured"]
    assert evidence["provenance"] == "MEASURED"
    assert evidence["profile_id"] == run["id"]
    assert evidence["function_name"] == "reconcile"
    assert evidence["hotspot_file_path"] == "shopfast/inventory_sync.py"
    assert evidence["hotspot_line"] == 8
    static_score = static[nested["id"]]["priority_score"]
    factor = 1 + 2 * evidence["attributed_percentage"] / 100
    assert evidence["measured_factor"] == pytest.approx(factor, abs=1e-3)
    path_factor = nested["priority_factors"]["exercised_path_factor"]
    assert path_factor >= 1.0
    assert nested["priority_score"] == pytest.approx(
        round(static_score * path_factor * factor, 2), abs=0.02
    )
    assert nested["priority_factors"]["static_priority_score"] == static_score
    assert findings[0]["id"] == nested["id"]  # measured re-ranking puts the real hotspot first

    expected_correlated = {
        ("PP-DB-001", "shopfast/orders.py", 13): "order_summaries",  # N+1 query in a loop
        ("PP-DB-001", "shopfast/orders.py", 19): "order_summaries",
        ("PP-LOOP-001", "shopfast/inventory_sync.py", 42): "duplicate_skus",
        ("PP-COLL-001", "shopfast/inventory_sync.py", 42): "duplicate_skus",
        ("PP-LOOP-001", "shopfast/inventory_sync.py", 33): "restock_order",
        ("PP-COLL-002", "shopfast/inventory_sync.py", 32): "restock_order",
        ("PP-LOOP-001", "shopfast/catalog.py", 41): "filter_discontinued",
        ("PP-CACHE-001", "shopfast/utils.py", 8): "bundle_combinations",
    }
    for key, function in expected_correlated.items():
        assert by_key[key]["measured"]["function_name"] == function, key
        assert by_key[key]["provenance"] == "STATIC"
    # The library time of list.count/list.pop is attributed to the functions that call it.
    dupes = by_key[("PP-COLL-001", "shopfast/inventory_sync.py", 42)]["measured"]
    assert dupes["attributed_time_ms"] > dupes["internal_time_ms"]
    # Functions the workload never called keep their static score and get no evidence.
    for key in (
        ("PP-ASYNC-001", "shopfast/api.py", 31),
        ("PP-ALLOC-001", "shopfast/orders.py", 52),
    ):
        assert by_key[key]["measured"] is None
        assert by_key[key]["priority_score"] == static[by_key[key]["id"]]["priority_score"]

    spans = {c["finding_id"]: c["location"] for c in run["correlations"]}
    span = spans[nested["id"]]
    assert (span["symbol"], span["line_start"], span["line_end"]) == ("reconcile", 8, 14)
    runtime_only = {r["function_name"]: r for r in run["runtime_only_hotspots"]}
    assert "build_catalog" in runtime_only  # hot, but no static finding
    assert all(r["location"]["match"] != "module" for r in run["runtime_only_hotspots"])
    assert all(r["attributed_percentage"] >= 1.0 for r in run["runtime_only_hotspots"])

    overview = client.get(f"/api/audits/{audit_id}/runtime").json()
    assert overview["measured_run_id"] == run["id"]
    assert overview["stage"] == "COMPLETED"
    package = [h for h in overview["hotspots"] if h["file_path"].startswith("shopfast/")]
    assert package[0]["function_name"] == "reconcile"
    assert len(overview["correlations"]) == len(run["correlations"])
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["summary"]["runtime"]["state"] == "measured"
    assert audit["summary"]["runtime"]["measured_run_id"] == run["id"]


def test_failing_tests_are_recorded_without_hotspots(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    snapshot = shopfast_snapshot(settings, extra_files={"tests/test_zz_fail.py": FAILING_TEST})
    audit_id = start_runtime_audit(client, settings, snapshot)
    run = _run(client, settings, audit_id, ["python", "-m", "pytest", "-q"])

    harness_request = json.loads((harness_backend.scratch / "request-1.json").read_text())
    assert harness_request["command"]["kind"] == "pytest"
    assert harness_request["command"]["args"] == ["-q"]
    assert run["_job"]["status"] == "SUCCEEDED"  # the job worked; the command failed
    assert run["status"] == "FAILED"
    assert run["command_exit_code"] == 1
    assert "exited with code 1 (test failures or errors)" in run["failure_reason"]
    assert run["hotspots"] == []
    assert run["correlations"] == []
    assert run["wall_time_ms"] is None
    assert "test_always_fails" in run["stdout_tail"]
    assert run["artifacts"]["harness_result_json"] is not None
    assert all(f["measured"] is None for f in _findings(client, audit_id))
    overview = client.get(f"/api/audits/{audit_id}/runtime").json()
    assert overview["stage"] == "AWAITING_RUNTIME_APPROVAL"
    assert overview["measured_run_id"] is None
    assert any("produced no measurements" in w for w in overview["warnings"])


@pytest.mark.parametrize(
    ("result", "status", "reason"),
    [
        (
            SandboxRunResult(
                exit_code=137,
                timed_out=False,
                oom_killed=True,
                duration_s=3.0,
                stdout="",
                stderr="",
            ),
            "OOM_KILLED",
            "memory limit",
        ),
        (
            SandboxRunResult(
                exit_code=137,
                timed_out=True,
                oom_killed=False,
                duration_s=9.0,
                stdout="",
                stderr="",
            ),
            "TIMED_OUT",
            "wall-clock limit",
        ),
        (
            SandboxRunResult(
                exit_code=1,
                timed_out=False,
                oom_killed=False,
                duration_s=1.0,
                stdout="",
                stderr="boom",
            ),
            "FAILED",
            "harness produced no result",
        ),
    ],
)
def test_container_outcomes_are_recorded_honestly(
    client: TestClient,
    settings: Settings,
    use_backend: BackendSetter,
    result: SandboxRunResult,
    status: str,
    reason: str,
) -> None:
    backend = use_backend(ScriptedBackend(result=result))
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    run = _run(client, settings, audit_id, WORKLOAD_ARGV)
    assert len(backend.runs) == 1  # type: ignore[attr-defined]
    assert run["status"] == status
    assert reason in run["failure_reason"]
    assert run["timed_out"] is result.timed_out
    assert run["oom_killed"] is result.oom_killed
    assert run["hotspots"] == []
    assert all(f["measured"] is None for f in _findings(client, audit_id))


def test_backend_unavailable_at_run_time(
    client: TestClient, settings: Settings, use_backend: BackendSetter, tmp_path: Path
) -> None:
    use_backend(SubprocessHarnessBackend(scratch=tmp_path / "s"))
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    accepted = approve(client, audit_id, command_by_argv(client, audit_id, WORKLOAD_ARGV))
    use_backend(UnavailableBackend(reason="docker daemon not reachable (test double)"))
    drain(settings)

    job = client.get(f"/api/jobs/{accepted['job_id']}").json()
    assert job["status"] == "FAILED"
    assert job["error_details"]["code"] == "unavailable"
    run = client.get(f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}").json()
    assert run["status"] == "UNAVAILABLE"
    assert "docker daemon not reachable" in run["failure_reason"]
    with session_scope() as session:
        approval = session.get(RuntimeApproval, run["approval_id"])
        assert approval is not None
        assert approval.status == "REVOKED"
    status = client.get("/api/runtime/status").json()
    assert status["available"] is False
    audit = client.get(f"/api/audits/{audit_id}").json()
    assert audit["status"] == "SUCCEEDED"
    assert all(f["measured"] is None for f in _findings(client, audit_id))


def test_separately_approved_dependency_step_runs_first(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    run = _run(
        client, settings, audit_id, ["python", "-m", "pytest", "-q"], allow_dependency_install=True
    )

    [install] = harness_backend.installs
    assert install.allow_sdists is False
    assert install.manifest_file.read_text().split() == ["pytest>=8"]  # pyproject [test] extra
    [request] = harness_backend.runs
    assert request.deps_dir is not None
    assert (request.deps_dir / "installed-by-test-double").is_dir()
    assert run["status"] == "SUCCEEDED", run["failure_reason"]
    assert run["dependency_install"]["succeeded"] is True
    assert run["dependency_install"]["source"]["kind"] == "pyproject"
    assert run["environment"]["dependencies"] == "prepared /deps (read-only)"
    with session_scope() as session:
        approval = session.get(RuntimeApproval, run["approval_id"])
        assert approval is not None
        assert approval.dependency_install_consumed_at is not None
        assert approval.status == "CONSUMED"


def test_dependency_step_without_manifest_fails_cleanly(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    workspace = settings.workspaces_dir / "shopfast"
    (workspace / "pyproject.toml").write_text('[project]\nname = "shopfast"\n')
    run = _run(client, settings, audit_id, WORKLOAD_ARGV, allow_dependency_install=True)
    assert run["status"] == "FAILED"
    assert "no requirements.txt or PEP 621" in run["failure_reason"]
    assert harness_backend.runs == []
    assert harness_backend.installs == []
    with session_scope() as session:
        approval = session.get(RuntimeApproval, run["approval_id"])
        assert approval is not None
        assert approval.status == "REVOKED"  # never executed, and can no longer be used


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["python", "-m", "pytest"], ("pytest", ".", [])),
        (["python", "-m", "pytest", "-q", "tests"], ("pytest", "tests", ["-q"])),
        (
            ["python", "-m", "pytest", "-k", "fast or slow", "tests/a.py", "tests/b.py::t", "-x"],
            ("pytest", "tests/a.py", ["-k", "fast or slow", "tests/b.py::t", "-x"]),
        ),
        (["python", "-m", "shopfast.app"], ("module", "shopfast.app", [])),
        (["python", "benchmarks/run_workload.py"], ("script", "benchmarks/run_workload.py", [])),
    ],
)
def test_harness_command_mapping(argv: list[str], expected: tuple[str, str, list[str]]) -> None:
    command = harness_command(argv, trace_memory=False)
    assert (command["kind"], command["target"], command["args"]) == expected
    assert command["stats_json"] is True
    assert command["trace_memory"] is False


def test_harness_command_rejects_other_shapes() -> None:
    for argv in (["python"], ["python", "-c", "x"], ["sh", "-c", "x"], ["python", "a.py", "b"]):
        with pytest.raises(Exception, match="executable shape"):
            harness_command(argv, trace_memory=True)


def test_choose_requirements_source(tmp_path: Path) -> None:
    assert choose_requirements_source(tmp_path, ".") is None
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["a"]\n'
        '[project.optional-dependencies]\ntest = ["b"]\n'
    )
    source = choose_requirements_source(tmp_path, ".")
    assert source is not None
    assert (source.kind, source.path_rel, source.extras) == (
        "pyproject",
        "pyproject.toml",
        ["test"],
    )
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "requirements.txt").write_text("c\n")
    source = choose_requirements_source(tmp_path, "sub")
    assert source is not None
    assert (source.kind, source.path_rel) == ("requirements_file", "sub/requirements.txt")
    (tmp_path / "pyproject.toml").write_text("not = [toml")
    assert choose_requirements_source(tmp_path, ".") is None


def test_outcome_requires_a_clean_harness_result() -> None:
    ok = SandboxRunResult(
        exit_code=0, timed_out=False, oom_killed=False, duration_s=1, stdout="", stderr=""
    )
    good = {"status": "ok", "command": {"kind": "script", "exit_code": 0}}
    assert outcome_of(ok, good)[0] == "SUCCEEDED"
    assert outcome_of(ok, None)[0] == "FAILED"
    raised = {
        "status": "error",
        "error": {"type": "ValueError", "message": "bad token ghp_" + "A" * 36},
    }
    status, reason = outcome_of(ok, raised)
    assert status == "FAILED"
    assert "ValueError" in reason
    assert "ghp_" not in reason  # secrets are redacted
    assert (
        outcome_of(ok, {"status": "ok", "command": {"exit_code": 3}})[1]
        == "the command exited with code 3"
    )
    nonzero = ok.model_copy(update={"exit_code": 2})
    assert outcome_of(nonzero, good)[0] == "FAILED"


@dataclass
class DockerArgvRecorder(SubprocessHarnessBackend):
    """TEST DOUBLE: also builds the real ``docker run`` argv for each request (never executed)."""

    docker: DockerSandboxBackend | None = None
    docker_argv: list[list[str]] = field(default_factory=list)

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        assert self.docker is not None
        self.docker_argv.append(self.docker.build_run_argv(request, container_name="pp-test"))
        return super().run(request)


def test_worker_request_passes_the_real_docker_path_policy(
    client: TestClient, settings: Settings, use_backend: BackendSetter, tmp_path: Path
) -> None:
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    docker = DockerSandboxBackend(settings.model_copy(update={"sandbox_docker_path": stub}))
    backend = use_backend(DockerArgvRecorder(scratch=tmp_path / "scratch", docker=docker))
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    run = _run(client, settings, audit_id, WORKLOAD_ARGV)
    assert run["status"] == "SUCCEEDED"

    [argv] = backend.docker_argv  # type: ignore[attr-defined]
    workspace = (settings.workspaces_dir / "shopfast").resolve()
    out = (settings.artifacts_dir / "runtime-runs" / run["id"] / "out").resolve()
    request_file = out.parent / "in" / "request.json"
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--user") + 1] == "65534:65534"
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts == [
        f"type=bind,source={workspace},target=/workspace,readonly",
        f"type=bind,source={out},target=/out",
        f"type=bind,source={HARNESS_PATH},target=/perfpilot/harness_entry.py,readonly",
        f"type=bind,source={request_file},target=/perfpilot/request.json,readonly",
    ]
    image_at = argv.index(settings.sandbox_image)
    assert argv[image_at - 2 : image_at] == ["--workdir", "/workspace"]
    assert argv[image_at + 1 :] == [
        "python",
        "-P",
        "-s",
        "-B",
        "/perfpilot/harness_entry.py",
        "/perfpilot/request.json",
        "/out/result.json",
    ]
    assert "--env" in argv
    assert "PYTHONHASHSEED=0" in argv


def test_command_output_is_redacted_before_it_is_stored(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    token = "ghp_" + "Z" * 36
    leak = f"if __name__ == '__main__':\n    print('token {token}')\n"
    snapshot = shopfast_snapshot(settings, extra_files={"benchmarks/leak.py": leak})
    audit_id = start_runtime_audit(client, settings, snapshot)
    run = _run(client, settings, audit_id, ["python", "benchmarks/leak.py"])
    assert run["status"] == "SUCCEEDED"
    assert "token" in run["stdout_tail"]
    assert token not in json.dumps(run)


def test_missing_workspace_fails_the_run_before_anything_runs(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    accepted = approve(client, audit_id, command_by_argv(client, audit_id, WORKLOAD_ARGV))
    shutil.rmtree(settings.workspaces_dir / "shopfast")
    drain(settings)
    assert client.get(f"/api/jobs/{accepted['job_id']}").json()["status"] == "FAILED"
    run = client.get(f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}").json()
    assert run["status"] == "FAILED"
    assert "no longer exists" in run["failure_reason"]
    assert harness_backend.runs == []
    with session_scope() as session:
        approval = session.get(RuntimeApproval, run["approval_id"])
        assert approval is not None
        assert approval.status == "REVOKED"
