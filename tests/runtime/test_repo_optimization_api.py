"""Repository optimization candidates through the API (ShopFast fixture).

Sandbox execution uses the ``SubprocessHarnessBackend`` TEST DOUBLE from ``runtime_support``: it
replaces only the container boundary and runs the real harness, so the test outcomes, cProfile
data and timings asserted here are real. The real-Docker flow is in
``test_repo_optimization_docker_e2e.py``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from llm_support import FakeLLM, client_factory
from repo_optimization_support import (
    PYTEST_ARGV,
    VALID_CLASSIFICATIONS,
    FixedProposer,
    approve_tests_and_workload,
    benchmark,
    candidate,
    create,
    finding,
    profile,
    reverse_reconcile,
    validate,
)
from runtime_support import (
    WORKLOAD_ARGV,
    SubprocessHarnessBackend,
    drain,
    shopfast_snapshot,
    start_runtime_audit,
)

from backend.ai import llm_client
from backend.core.config import Settings, get_settings, reset_settings_cache
from backend.models.database import session_scope
from backend.models.enums import ReportFormat
from backend.models.orm.audit import RepositorySnapshot
from backend.optimization.patch_generator import register_patch_proposer
from backend.reporting.builder import build_audit_report
from backend.reporting.renderers import render_report
from backend.runtime.backends import record_backend_status
from backend.sandbox.backend import BackendStatus
from backend.services.source_snapshots import hash_tree

INVENTORY = "shopfast/inventory_sync.py"
Install = Callable[[str, Callable[[str], str]], None]


def _snapshot_root(client: TestClient, audit_id: str) -> Path:
    snapshot_id = client.get(f"/api/audits/{audit_id}").json()["snapshot_id"]
    with session_scope() as session:
        snapshot = session.get(RepositorySnapshot, snapshot_id)
        assert snapshot is not None
        assert snapshot.workspace_path is not None
        return Path(snapshot.workspace_path)


def _error(response: Any) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


def _approve(client: TestClient, candidate_id: str) -> None:
    response = client.post(
        f"/api/repo-optimizations/{candidate_id}/approve", json={"approved_by": "reviewer"}
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def custom_proposer() -> Iterator[Install]:
    def install(path: str, transform: Callable[[str], str]) -> None:
        register_patch_proposer(lambda _settings: FixedProposer(path, transform))

    yield install
    register_patch_proposer(None)


@pytest.fixture
def measured_audit(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> str:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    approve_tests_and_workload(client, settings, audit_id)
    return audit_id


def test_measured_nested_loop_candidate_is_validated_benchmarked_and_reported(
    client: TestClient,
    settings: Settings,
    measured_audit: str,
    harness_backend: SubprocessHarnessBackend,
) -> None:
    audit_id = measured_audit
    root = _snapshot_root(client, audit_id)
    snapshot_hashes = hash_tree(root)
    nested = finding(client, audit_id, "PP-LOOP-002", INVENTORY)
    assert nested["measured"]["provenance"] == "MEASURED"

    created = create(client, audit_id, nested["id"])
    assert created["state"] == "AWAITING_APPROVAL", created["error"]
    assert created["outcome"] == "proposed"
    assert created["evidence"] == "MEASURED"
    assert created["measured_evidence"]["function_name"] == "reconcile"
    assert (created["generated_by"], created["provenance"]) == ("deterministic", "STATIC")
    assert created["transform"] == "nested_loop_join_to_index"
    assert created["patch_validation"]["valid"] is True
    assert [h["to_state"] for h in created["state_history"]] == [
        "PATCH_PROPOSED",
        "AWAITING_APPROVAL",
    ]

    # The patched file exists only in the isolated copy-on-write workspace.
    src = Path(created["workspace_path"])
    assert src == settings.workspaces_dir / "repo-optimizations" / created["id"] / "src"
    assert hash_tree(root) == snapshot_hashes
    diff = client.get(f"/api/repo-optimizations/{created['id']}/diff").json()
    assert diff["unified_diff"].startswith(f"--- a/{INVENTORY}\n+++ b/{INVENTORY}\n")
    [file] = diff["files"]
    assert file["original"] == (root / INVENTORY).read_text()
    assert file["patched"] == (src / INVENTORY).read_text() != file["original"]
    assert os.stat(src / INVENTORY).st_ino != os.stat(root / INVENTORY).st_ino
    linked = "shopfast/catalog.py"
    assert os.stat(src / linked).st_ino == os.stat(root / linked).st_ino

    # Nothing runs before an explicit approval.
    response = client.post(f"/api/repo-optimizations/{created['id']}/validate")
    assert response.status_code == 409
    assert _error(response)["code"] == "invalid_state_transition"
    response = client.post(f"/api/repo-optimizations/{created['id']}/benchmark", json={})
    assert _error(response)["code"] == "invalid_state_transition"
    _approve(client, created["id"])
    assert candidate(client, created["id"])["approval"]["approved_by"] == "reviewer"

    runs_before = len(harness_backend.runs)
    validated = validate(client, settings, created["id"])
    validation = validated["validation"]
    assert validation["status"] == "PASSED", validation["summary"]
    assert validated["state"] == "VALIDATED"
    assert validation["argv"] == PYTEST_ARGV
    assert validation["test_comparison"]["original_passed"] == 24
    assert validation["test_comparison"]["patched_passed"] == 24
    assert validation["test_comparison"]["regression_count"] == 0
    assert validation["patched_function_executed"] is True
    checks = {c["name"]: c["status"] for c in validation["checks"]}
    assert checks == {
        "isolation": "PASSED",
        "compile": "PASSED",
        "original_suite": "PASSED",
        "patched_suite": "PASSED",
        "patched_code_exercised": "PASSED",
    }
    original_run, patched_run = harness_backend.runs[runs_before:]
    assert original_run.workspace_dir == root.resolve()
    assert patched_run.workspace_dir == src
    assert (original_run.limits, original_run.image) == (patched_run.limits, patched_run.image)
    assert original_run.network == patched_run.network == "none"
    assert validation["environment"]["identical_requests"] is True
    assert validation["logs_artifact_id"]
    junit_args = harness_backend.harness_requests[-1]["command"]["args"]
    assert junit_args == ["-q", "--junitxml=/out/junit.xml"]

    runs_before = len(harness_backend.runs)
    benchmarked = benchmark(client, settings, created["id"], warmups=1, runs=3, seed=7)
    result = benchmarked["benchmark"]
    assert result["status"] == "SUCCEEDED", result["error"]
    assert result["classification"] in VALID_CLASSIFICATIONS
    assert benchmarked["state"] == "COMPLETED"
    assert benchmarked["provenance"] == "MEASURED"
    assert result["argv"] == WORKLOAD_ARGV
    assert result["inputs"]["differing_files"] == [INVENTORY]
    assert result["inputs"]["baseline"]["workspace_tree_sha256"] == snapshot_hashes[0]
    assert len(result["samples"]) == 8
    measured = [s for s in result["samples"] if not s["warmup"]]
    assert sorted((s["variant"], s["pair_index"]) for s in measured) == [
        (v, i) for v in ("baseline", "candidate") for i in range(3)
    ]
    assert all(s["ok"] and s["wall_ns"] > 0 and s["peak_rss_bytes"] > 0 for s in measured)
    summary = result["summary"]
    assert summary["baseline"]["wall_ms"]["n"] == summary["candidate"]["wall_ms"]["n"] == 3
    assert result["speedup"] == pytest.approx(
        summary["baseline"]["wall_ms"]["median"] / summary["candidate"]["wall_ms"]["median"]
    )
    low, high = result["speedup_ci"]
    assert low <= high
    assert "excluded" in result["timed"]
    timed_requests = harness_backend.harness_requests[-8:]
    assert all(r["command"]["profile"] is False for r in timed_requests)
    assert len(harness_backend.runs) - runs_before == 8
    assert hash_tree(root) == snapshot_hashes

    listed = client.get(f"/api/audits/{audit_id}/optimizations").json()
    assert [c["id"] for c in listed] == [created["id"]]

    with session_scope() as session:
        report = build_audit_report(session, audit_id)
    assert report.metadata.providers_applied == ["repo-optimization", "runtime-audit"]
    change = report.code_changes.entries[0]
    assert change.provenance == "MEASURED"
    assert change.diff is not None
    assert change.diff.startswith(f"--- a/{INVENTORY}")
    assert report.validation is not None
    [outcome] = report.validation.outcomes
    assert (outcome.status, outcome.command) == ("PASSED", PYTEST_ARGV)
    assert report.benchmarks is not None
    [bench] = report.benchmarks.results
    assert bench.classification == result["classification"]
    assert len(bench.samples) == 6
    assert bench.baseline_median_ms == summary["baseline"]["wall_ms"]["median"]
    rendered = render_report(report, ReportFormat.CSV)
    samples_csv = next(f for f in rendered.files if f.role == "benchmark_samples")
    assert len(samples_csv.data.decode().strip().splitlines()) == 7  # header + 6 samples


def test_wrong_candidate_fails_validation_and_cannot_be_benchmarked(
    client: TestClient, settings: Settings, measured_audit: str, custom_proposer: Install
) -> None:
    custom_proposer(INVENTORY, reverse_reconcile)
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    assert created["generated_by"] == "test-double-proposer"
    assert created["provenance"] == "AI_HYPOTHESIS"
    assert created["state"] == "AWAITING_APPROVAL"
    _approve(client, created["id"])

    failed = validate(client, settings, created["id"])
    assert failed["state"] == "FAILED"
    validation = failed["validation"]
    assert validation["status"] == "FAILED"
    regressions = validation["test_comparison"]["regressions"]
    expected = {"test": "tests.test_inventory_sync::test_reconcile_variants_agree"}
    assert expected | {"patched": "failed"} in regressions
    response = client.post(f"/api/repo-optimizations/{created['id']}/benchmark", json={})
    assert response.status_code == 409
    assert _error(response)["code"] == "invalid_state_transition"


def test_validation_and_benchmark_need_commands_the_user_approved(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    profile(client, settings, audit_id, WORKLOAD_ARGV)
    nested = finding(client, audit_id, "PP-LOOP-002", INVENTORY)
    created = create(client, audit_id, nested["id"])
    _approve(client, created["id"])
    runs_before = len(harness_backend.runs)

    response = client.post(f"/api/repo-optimizations/{created['id']}/validate")
    assert response.status_code == 409
    assert "no approved test command" in _error(response)["message"]
    assert candidate(client, created["id"])["state"] == "AWAITING_APPROVAL"
    assert len(harness_backend.runs) == runs_before  # nothing was executed

    profile(client, settings, audit_id, PYTEST_ARGV)
    validated = validate(client, settings, created["id"])
    assert validated["state"] == "VALIDATED", validated["validation"]["summary"]


def test_benchmark_needs_an_approved_workload_command(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    profile(client, settings, audit_id, PYTEST_ARGV)
    nested = finding(client, audit_id, "PP-LOOP-002", INVENTORY)
    created = create(client, audit_id, nested["id"])
    _approve(client, created["id"])
    assert validate(client, settings, created["id"])["state"] == "VALIDATED"

    response = client.post(f"/api/repo-optimizations/{created['id']}/benchmark", json={})
    assert response.status_code == 409
    assert "no approved workload command" in _error(response)["message"]
    assert candidate(client, created["id"])["state"] == "VALIDATED"


def test_static_only_finding_is_allowed_but_labeled(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    response = client.post(
        "/api/audits", json={"snapshot_id": shopfast_snapshot(settings), "mode": "STATIC"}
    )
    audit_id = response.json()["subject_id"]
    drain(settings)
    membership = finding(client, audit_id, "PP-LOOP-001", "shopfast/catalog.py", 41)
    assert membership["measured"] is None
    created = create(client, audit_id, membership["id"])
    assert created["state"] == "AWAITING_APPROVAL", created["error"]
    assert (created["evidence"], created["measured_evidence"]) == ("STATIC", None)
    assert created["transform"] == "list_membership_to_set"
    _approve(client, created["id"])
    response = client.post(f"/api/repo-optimizations/{created['id']}/validate")
    assert response.status_code == 409
    assert "no approved test command" in _error(response)["message"]
    assert harness_backend.runs == []


def test_no_safe_patch_is_a_clear_outcome(client: TestClient, measured_audit: str) -> None:
    n_plus_one = finding(client, measured_audit, "PP-DB-001", "shopfast/orders.py", 13)
    created = create(client, measured_audit, n_plus_one["id"])
    assert created["outcome"] == "no_safe_patch"
    assert created["state"] == "FAILED"
    assert "no safe deterministic patch" in created["error"]
    assert created["workspace_path"] is None
    assert client.get(f"/api/repo-optimizations/{created['id']}/diff").status_code == 404
    with session_scope() as session:
        report = build_audit_report(session, measured_audit)
    assert any("No patch candidate for finding" in item for item in report.limitations.limitations)


@pytest.mark.parametrize(
    ("path", "codes"),
    [
        ("tests/test_inventory_sync.py", {"protected_path", "not_allowed"}),
        ("benchmarks/run_workload.py", {"protected_path", "not_allowed"}),
        ("shopfast/catalog.py", {"not_allowed"}),
    ],
)
def test_patch_validator_rejects_changes_outside_the_finding_file(
    client: TestClient,
    measured_audit: str,
    custom_proposer: Install,
    path: str,
    codes: set[str],
) -> None:
    custom_proposer(path, lambda text: text + "\n# tampered\n")
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    assert created["state"] == "REJECTED"
    assert created["outcome"] == "rejected_by_validator"
    assert created["approval"]["rejected_by"] == "patch-validator"
    assert codes <= {r["code"] for r in created["rejection_reasons"]}
    response = client.post(f"/api/repo-optimizations/{created['id']}/approve", json={})
    assert response.status_code == 409


def test_benchmark_gaming_patch_is_rejected(
    client: TestClient, measured_audit: str, custom_proposer: Install
) -> None:
    custom_proposer(
        INVENTORY,
        lambda text: text.replace(
            "def reconcile(", "import functools\n\n\n@functools.lru_cache\ndef reconcile(", 1
        ),
    )
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    assert created["state"] == "REJECTED"
    assert "benchmark_tampering" in {r["code"] for r in created["rejection_reasons"]}


def test_rejection_is_final_and_removes_the_workspace(
    client: TestClient, measured_audit: str
) -> None:
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    workspace = Path(created["workspace_path"])
    assert workspace.is_dir()
    rejected = client.post(
        f"/api/repo-optimizations/{created['id']}/reject",
        json={"reason": "prefer a different fix", "rejected_by": "reviewer"},
    ).json()
    assert rejected["state"] == "REJECTED"
    assert rejected["outcome"] == "rejected"
    assert rejected["rejection_reasons"][-1]["reason"] == "prefer a different fix"
    assert not workspace.exists()
    response = client.post(f"/api/repo-optimizations/{created['id']}/approve", json={})
    assert _error(response)["code"] == "invalid_state_transition"


def test_changed_snapshot_is_detected(client: TestClient, measured_audit: str) -> None:
    root = _snapshot_root(client, measured_audit)
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    (root / "shopfast/utils.py").write_text("# edited after the audit\n")
    response = client.post(f"/api/repo-optimizations/{created['id']}/approve", json={})
    assert _error(response)["code"] == "original_tree_changed"
    assert candidate(client, created["id"])["state"] == "FAILED"

    (root / INVENTORY).write_text((root / INVENTORY).read_text() + "\n# edited\n")
    response = client.post(
        f"/api/audits/{measured_audit}/optimizations", json={"finding_id": nested["id"]}
    )
    assert response.status_code == 409
    assert _error(response)["code"] == "source_changed"


def test_unavailable_sandbox_refuses_validation(
    client: TestClient, settings: Settings, measured_audit: str
) -> None:
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    _approve(client, created["id"])
    with session_scope() as session:
        record_backend_status(
            session,
            settings,
            BackendStatus(available=False, backend="docker", reason="daemon stopped (test)"),
        )
    response = client.post(f"/api/repo-optimizations/{created['id']}/validate")
    assert response.status_code == 503
    assert "daemon stopped" in _error(response)["message"]
    assert candidate(client, created["id"])["state"] == "AWAITING_APPROVAL"


def test_unknown_ids_and_bad_requests(client: TestClient, measured_audit: str) -> None:
    url = f"/api/audits/{measured_audit}/optimizations"
    assert client.post(url, json={"finding_id": "nope"}).status_code == 404
    assert client.post(url, json={"finding_id": "../etc/passwd"}).status_code == 422
    assert client.post(url, json={"finding_id": "x", "extra": 1}).status_code == 422
    assert client.get("/api/repo-optimizations/unknown").status_code == 404
    assert client.get("/api/audits/unknown/optimizations").status_code == 404
    nested = finding(client, measured_audit, "PP-LOOP-002", INVENTORY)
    created = create(client, measured_audit, nested["id"])
    _approve(client, created["id"])
    bench = f"/api/repo-optimizations/{created['id']}/benchmark"
    assert client.post(bench, json={"runs": 1}).status_code == 422
    assert client.post(bench, json={"warmups": 11}).status_code == 422


RESTOCK_REWRITE = """def restock_order(events: list[str]) -> list[str]:
    processed: list[str] = []
    seen: set[str] = set()
    for sku in events:
        if sku in seen:
            continue
        seen.add(sku)
        processed.append(sku)
    return processed
"""


@pytest.fixture
def fake_llm(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    monkeypatch.setenv("PERFPILOT_LLM_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("PERFPILOT_LLM_MODEL", "fake-model")
    reset_settings_cache()
    fake = FakeLLM(lambda _step, _user: {"replacement": RESTOCK_REWRITE, "summary": "set lookup"})
    monkeypatch.setattr(llm_client, "create_llm_client", client_factory(fake))
    return fake


@pytest.mark.parametrize("consent", [True, False])
def test_llm_proposer_needs_audit_level_consent(
    client: TestClient,
    settings: Settings,
    harness_backend: SubprocessHarnessBackend,
    fake_llm: FakeLLM,
    consent: bool,
) -> None:
    settings = get_settings()
    response = client.post(
        "/api/audits",
        json={
            "snapshot_id": shopfast_snapshot(settings),
            "mode": "RUNTIME",
            "llm_consent": consent,
        },
    )
    audit_id = response.json()["subject_id"]
    drain(settings)
    profile(client, settings, audit_id, PYTEST_ARGV)
    pop_front = finding(client, audit_id, "PP-COLL-002", INVENTORY)
    created = create(client, audit_id, pop_front["id"])
    if not consent:
        assert fake_llm.calls == 0
        assert created["outcome"] == "no_safe_patch"
        assert created["generated_by"] == "deterministic"
        return
    assert fake_llm.calls == 1
    assert created["state"] == "AWAITING_APPROVAL", created["error"]
    assert (created["generated_by"], created["provenance"]) == ("llm:fake-model", "AI_HYPOTHESIS")
    assert created["transform"] == "llm-function-rewrite"
    assert created["proposer_attempts"][0].startswith("deterministic: no safe deterministic patch")
    assert created["llm_usage"]["calls"] == 1
    sent = fake_llm.sent_text()
    assert "queue.pop(0)" in sent
    assert "def reconcile" not in sent  # only the target function leaves the machine
    _approve(client, created["id"])
    validated = validate(client, settings, created["id"])
    assert validated["validation"]["status"] == "PASSED", validated["validation"]["summary"]
    assert validated["provenance"] == "AI_HYPOTHESIS"  # validated, but not benchmarked yet
