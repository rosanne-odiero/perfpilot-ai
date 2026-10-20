"""Repository optimization deep dive against the REAL Docker sandbox (``@pytest.mark.docker``).

Import ShopFast as a local directory -> RUNTIME audit -> approve and profile the pytest command and
the workload command -> candidate for the measured nested-loop finding -> approve -> validate in
the sandbox -> paired A/B benchmark in the sandbox -> report sections 10-12. A deliberately wrong
candidate (from a test-only proposer double) must fail validation and cannot be benchmarked.

Skipped with the reason unless a Docker daemon is reachable and ``perfpilot-sandbox:latest`` exists
(``make sandbox-image``). No sandbox test double is installed: the worker uses the real
``DockerSandboxBackend``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from audit_support import SHOPFAST
from fastapi.testclient import TestClient
from repo_optimization_support import (
    PYTEST_ARGV,
    VALID_CLASSIFICATIONS,
    FixedProposer,
    approve_tests_and_workload,
    benchmark,
    create,
    finding,
    reverse_reconcile,
    validate,
)
from runtime_support import WORKLOAD_ARGV, drain

from backend.core.config import Settings
from backend.models.database import session_scope
from backend.models.enums import ReportFormat
from backend.models.orm.audit import RepositorySnapshot
from backend.optimization.patch_generator import register_patch_proposer
from backend.reporting.builder import build_audit_report
from backend.reporting.renderers import render_report
from backend.runtime.backends import set_backend_factory
from backend.sandbox.docker_backend import CONTAINER_LABEL, find_docker_executable
from backend.services.source_snapshots import hash_tree

pytestmark = pytest.mark.docker

INVENTORY = "shopfast/inventory_sync.py"
MEASURED_BADGE = 'class="badge prov prov-measured"'


@pytest.fixture
def repos_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PERFPILOT_LOCAL_ALLOWLIST", str(root))
    set_backend_factory(None)  # the real Docker backend, never a test double
    return root


@pytest.fixture
def api(repos_dir: Path, client: TestClient) -> Iterator[TestClient]:
    yield client
    register_patch_proposer(None)


def _runtime_audit(api: TestClient, settings: Settings, repos_dir: Path) -> str:
    source = repos_dir / "shopfast"
    shutil.copytree(SHOPFAST, source, ignore=shutil.ignore_patterns("__pycache__"))
    imported = api.post(
        "/api/repositories", json={"source_type": "LOCAL_DIRECTORY", "local_path": str(source)}
    )
    assert imported.status_code == 202, imported.text
    drain(settings)
    snapshot_id = imported.json()["subject_id"]
    response = api.post("/api/audits", json={"snapshot_id": snapshot_id, "mode": "RUNTIME"})
    assert response.status_code == 202, response.text
    drain(settings)
    audit_id = str(response.json()["subject_id"])
    audit = api.get(f"/api/audits/{audit_id}").json()
    assert audit["summary"]["runtime"]["runtime_available"] is True, audit["warnings"]
    approve_tests_and_workload(api, settings, audit_id)
    return audit_id


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


def _section(page: str, number: int) -> str:
    return page.split(f'id="s{number}"')[1].split(f'id="s{number + 1}"')[0]


def _snapshot_root(api: TestClient, audit_id: str) -> Path:
    snapshot_id = api.get(f"/api/audits/{audit_id}").json()["snapshot_id"]
    with session_scope() as session:
        snapshot = session.get(RepositorySnapshot, snapshot_id)
        assert snapshot is not None
        assert snapshot.workspace_path is not None
        return Path(snapshot.workspace_path)


def test_measured_hotspot_is_patched_validated_and_benchmarked_in_the_sandbox(
    api: TestClient, settings: Settings, repos_dir: Path
) -> None:
    audit_id = _runtime_audit(api, settings, repos_dir)
    before = set(_leftover_containers())
    root = _snapshot_root(api, audit_id)
    snapshot_hashes = hash_tree(root)
    nested = finding(api, audit_id, "PP-LOOP-002", INVENTORY)
    assert nested["measured"]["provenance"] == "MEASURED"
    assert nested["measured"]["function_name"] == "reconcile"

    created = create(api, audit_id, nested["id"])
    assert created["state"] == "AWAITING_APPROVAL", created["error"]
    assert created["evidence"] == "MEASURED"
    approved = api.post(
        f"/api/repo-optimizations/{created['id']}/approve", json={"approved_by": "e2e"}
    )
    assert approved.status_code == 200, approved.text

    validated = validate(api, settings, created["id"])
    validation = validated["validation"]
    assert validation["status"] == "PASSED", validation["summary"]
    assert validated["state"] == "VALIDATED"
    assert validation["argv"] == PYTEST_ARGV
    comparison = validation["test_comparison"]
    assert comparison["original_passed"] == comparison["patched_passed"] == 24
    assert validation["patched_function_executed"] is True
    env = validation["environment"]
    assert env["backend"] == "docker"
    assert env["image_digest"].startswith("sha256:")
    assert env["network"] == "none"
    assert "Linux" in env["platform"]
    assert hash_tree(root) == snapshot_hashes

    benchmarked = benchmark(api, settings, created["id"], warmups=1, runs=10, seed=2026)
    result = benchmarked["benchmark"]
    assert result["status"] == "SUCCEEDED", result["error"]
    assert benchmarked["state"] == "COMPLETED"
    assert benchmarked["provenance"] == "MEASURED"
    assert result["classification"] in VALID_CLASSIFICATIONS, result["classification_reason"]
    assert result["argv"] == WORKLOAD_ARGV
    assert result["inputs"]["differing_files"] == [INVENTORY]
    assert result["environment"]["backend"] == "docker"
    assert result["environment"]["profiler"] == "none"
    samples = result["samples"]
    assert len(samples) == 22
    measured = [s for s in samples if not s["warmup"]]
    assert len(measured) == 20
    assert all(s["ok"] and s["wall_ns"] > 0 and s["peak_rss_bytes"] > 0 for s in measured)
    # Real, distinct measurements; the timed span excludes container start/stop.
    assert len({s["wall_ns"] for s in measured}) > 10
    assert all(s["wall_ns"] / 1e9 < s["container_duration_s"] for s in measured)
    summary = result["summary"]
    base, cand = summary["baseline"]["wall_ms"], summary["candidate"]["wall_ms"]
    assert result["speedup"] == pytest.approx(base["median"] / cand["median"])
    low, high = result["speedup_ci"]
    assert low <= result["speedup"] <= high
    assert hash_tree(root) == snapshot_hashes

    with session_scope() as session:
        report = build_audit_report(session, audit_id)
    assert "repo-optimization" in report.metadata.providers_applied
    page = render_report(report, ReportFormat.HTML).primary.data.decode()
    s10, s11, s12 = (_section(page, n) for n in (10, 11, 12))
    assert MEASURED_BADGE in s10
    assert "reconcile" in s10
    assert "--- a/shopfast/inventory_sync.py" in s10
    assert MEASURED_BADGE in s11
    assert "PASSED" in s11
    assert MEASURED_BADGE in s12
    assert result["classification"] in s12
    csv = render_report(report, ReportFormat.CSV)
    samples_csv = next(f for f in csv.files if f.role == "benchmark_samples")
    assert len(samples_csv.data.decode().strip().splitlines()) == 21

    # The real numbers, for the record (shown with -s).
    record = {
        "validation": validation["status"],
        "tests_passed": comparison["original_passed"],
        "median_baseline_ms": base["median"],
        "median_candidate_ms": cand["median"],
        "speedup": result["speedup"],
        "speedup_ci_95": result["speedup_ci"],
        "latency_reduction_percent": result["latency_reduction_percent"],
        "classification": result["classification"],
        "classification_reason": result["classification_reason"],
        "runs": result["runs"],
        "warmups": result["warmups"],
    }
    print(json.dumps(record, indent=2))
    assert set(_leftover_containers()) <= before  # every sandbox container was removed


def test_wrong_candidate_fails_validation_in_the_sandbox_and_is_not_benchmarked(
    api: TestClient, settings: Settings, repos_dir: Path
) -> None:
    audit_id = _runtime_audit(api, settings, repos_dir)
    register_patch_proposer(lambda _settings: FixedProposer(INVENTORY, reverse_reconcile))
    nested = finding(api, audit_id, "PP-LOOP-002", INVENTORY)
    created = create(api, audit_id, nested["id"])
    assert created["generated_by"] == "test-double-proposer"
    assert created["state"] == "AWAITING_APPROVAL"
    api.post(f"/api/repo-optimizations/{created['id']}/approve", json={"approved_by": "e2e"})

    failed = validate(api, settings, created["id"])
    assert failed["state"] == "FAILED"
    validation = failed["validation"]
    assert validation["status"] == "FAILED", validation["summary"]
    assert validation["environment"]["backend"] == "docker"
    regressed = {r["test"] for r in validation["test_comparison"]["regressions"]}
    assert "tests.test_inventory_sync::test_reconcile_variants_agree" in regressed

    response = api.post(f"/api/repo-optimizations/{created['id']}/benchmark", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"
