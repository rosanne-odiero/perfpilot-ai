from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.infrastructure.artifact_store import sha256_bytes
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.orm.optimization import OptimizationRecommendation
from backend.models.orm.workloads import BenchmarkRun, ProfileRun
from backend.optimization import candidate_resolver
from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_generator import (
    PatchProposal,
    PatchRequest,
    register_patch_proposer,
)
from backend.services.source_snapshots import create_snapshot, hash_tree
from backend.services.workloads import EXAMPLES_DIR, build_spec, get_workload

SMALL_BENCHMARK = {
    "size": "small",
    "warmups": 1,
    "runs": 5,
    "memory_runs": 0,
    "bootstrap_resamples": 1000,
}


def _workload(client: TestClient, slug: str) -> dict[str, Any]:
    project = client.get("/api/projects").json()[0]
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    return next(w for w in workloads if w["slug"] == slug)


def _drain(settings: Settings) -> None:
    Worker(settings, isolate=False).drain()


def _profile(client: TestClient, settings: Settings, workload_id: str) -> str:
    accepted = client.post("/api/profiles", json={"workload_id": workload_id, "size": "small"})
    assert accepted.status_code == 202
    _drain(settings)
    profile_id: str = accepted.json()["subject_id"]
    assert client.get(f"/api/profiles/{profile_id}").json()["status"] == "SUCCEEDED"
    return profile_id


def _analysis(client: TestClient, settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    accepted = client.post("/api/analyses", json=body)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["subject_type"] == "analysis_run"
    _drain(settings)
    assert client.get(f"/api/jobs/{accepted.json()['job_id']}").json()["status"] == "SUCCEEDED"
    analysis: dict[str, Any] = client.get(f"/api/analyses/{accepted.json()['subject_id']}").json()
    return analysis


def _candidate(client: TestClient, recommendation_id: str) -> dict[str, Any]:
    response = client.post("/api/optimizations", json={"recommendation_id": recommendation_id})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _validate(client: TestClient, settings: Settings, candidate_id: str) -> dict[str, Any]:
    accepted = client.post(f"/api/optimizations/{candidate_id}/validate")
    assert accepted.status_code == 202, accepted.text
    assert client.get(f"/api/optimizations/{candidate_id}").json()["state"] == "VALIDATING"
    _drain(settings)
    body: dict[str, Any] = client.get(f"/api/optimizations/{candidate_id}").json()
    return body


def _states(candidate: dict[str, Any]) -> list[str]:
    return [h["to_state"] for h in candidate["state_history"]]


def _registered_tree(client: TestClient, slug: str) -> tuple[Path, str]:
    detail = client.get(f"/api/workloads/{_workload(client, slug)['id']}").json()
    root = EXAMPLES_DIR / slug
    assert detail["tree_sha256"] == hash_tree(root)[0]
    return root, detail["tree_sha256"]


def test_duplicate_counter_deep_dive_end_to_end(client: TestClient, settings: Settings) -> None:
    workload = _workload(client, "duplicate_counter")
    root, tree = _registered_tree(client, "duplicate_counter")
    profile_id = _profile(client, settings, workload["id"])
    hotspot = client.get(f"/api/profiles/{profile_id}/hotspots", params={"scope": "user"}).json()[0]

    analysis = _analysis(
        client, settings, {"workload_id": workload["id"], "profile_id": profile_id}
    )
    assert analysis["state"] == "RECOMMENDED"
    assert _states(analysis) == ["PROFILED", "ANALYZING", "RECOMMENDED"]
    assert analysis["profile_requested"] is False
    assert analysis["findings"][0]["rule_id"] == "PP-COLL-001"
    assert analysis["findings"][0]["provenance"] == "STATIC"
    recommendations = client.get(f"/api/analyses/{analysis['id']}/recommendations").json()
    assert len(recommendations) == 1
    rec = recommendations[0]
    assert rec["id"] == analysis["recommendation_ids"][0]
    assert rec["rule_id"] == "PP-COLL-001"
    assert rec["target_file"] == "workload.py"
    assert rec["target_function"] == "count_duplicates"
    assert rec["source_lines"] == {"start": 13, "end": 13}
    evidence = rec["profiler_evidence"]
    assert evidence["provenance"] == "MEASURED"
    assert evidence["profile_id"] == profile_id
    assert evidence["target_function"]["cumulative_time_ms"] == round(
        hotspot["cumulative_time_ms"], 3
    )
    assert evidence["target_function"]["total_calls"] == hotspot["total_calls"]
    assert any("count" in h["function_name"] for h in evidence["largest_non_workspace_functions"])
    assert "[MEASURED]" in rec["observed_bottleneck"]
    assert rec["estimated_impact"]["level"] in {"high", "moderate", "low"}
    assert rec["estimated_impact"]["provenance"] == "AI_HYPOTHESIS"
    assert not any(ch.isdigit() for ch in rec["estimated_impact"]["level"])
    assert rec["provenance"]["profiler_evidence"] == "MEASURED"
    assert rec["provenance"]["expected_effect"] == "AI_HYPOTHESIS"
    assert rec["generated_by"] == "deterministic"
    assert rec["patch_strategy"]["deterministic_patch_available"] is True
    assert rec["validation_plan"]
    assert rec["correctness_risks"]

    candidate = _candidate(client, rec["id"])
    candidate_id = candidate["id"]
    assert candidate["state"] == "AWAITING_APPROVAL"
    assert _states(candidate) == ["PATCH_PROPOSED", "AWAITING_APPROVAL"]
    assert candidate["generated_by"] == "deterministic"
    assert candidate["patch_validation"]["valid"] is True
    assert candidate["approval"]["status"] == "PENDING"
    assert candidate["original_tree_sha256"] == tree
    diff = client.get(f"/api/optimizations/{candidate_id}/diff").json()
    assert "Counter(items)" in diff["unified_diff"]
    (file,) = diff["files"]
    assert file["original"] == (root / "workload.py").read_text()
    assert sha256_bytes(file["patched"].encode()) == file["patched_sha256"]

    # Nothing can be validated or benchmarked before an explicit approval.
    assert client.post(f"/api/optimizations/{candidate_id}/validate").status_code == 409
    early = client.post(
        "/api/benchmarks",
        json={"workload_id": workload["id"], "candidate": {"candidate_id": candidate_id}},
    )
    assert early.status_code == 409
    assert client.get(f"/api/optimizations/{candidate_id}/validation").status_code == 404

    approved = client.post(
        f"/api/optimizations/{candidate_id}/approve", json={"approved_by": "reviewer@example"}
    ).json()
    assert approved["approval"]["status"] == "APPROVED"
    assert approved["approval"]["approved_by"] == "reviewer@example"
    assert approved["approval"]["approved_at"]
    assert client.post(f"/api/optimizations/{candidate_id}/approve", json={}).status_code == 409

    validated = _validate(client, settings, candidate_id)
    assert validated["state"] == "VALIDATED", validated["validation"]
    validation = client.get(f"/api/optimizations/{candidate_id}/validation").json()
    assert validation["status"] == "PASSED"
    assert {c["name"] for c in validation["checks"] if c["required"]} >= {
        "isolation",
        "compile",
        "functional_tests",
        "output_equivalence",
        "edge_cases",
        "exception_parity",
        "input_mutation",
        "patched_code_exercised",
    }
    assert validation["logs_artifact_id"]
    assert validated["patched_workspace_path"].startswith(str(settings.workspaces_dir))
    assert (
        hash_tree(Path(validated["patched_workspace_path"]))[0] == validated["patched_tree_sha256"]
    )

    # A candidate validated for one workload cannot be compared with another's baseline.
    other = _workload(client, "list_lookup")
    wrong = client.post(
        "/api/benchmarks",
        json={"workload_id": other["id"], "candidate": {"candidate_id": candidate_id}},
    )
    assert wrong.status_code == 409
    assert client.get(f"/api/optimizations/{candidate_id}").json()["state"] == "VALIDATED"

    accepted = client.post(
        "/api/benchmarks",
        json={
            "workload_id": workload["id"],
            "candidate": {"candidate_id": candidate_id},
            **SMALL_BENCHMARK,
        },
    )
    assert accepted.status_code == 202, accepted.text
    assert client.get(f"/api/optimizations/{candidate_id}").json()["state"] == "BENCHMARKING"
    _drain(settings)
    benchmark = client.get(f"/api/benchmarks/{accepted.json()['subject_id']}").json()
    assert benchmark["status"] == "SUCCEEDED", benchmark["error"]
    assert benchmark["candidate"]["kind"] == "candidate"
    assert benchmark["candidate"]["candidate_id"] == candidate_id
    assert "validated patch candidate" in benchmark["candidate"]["label"]
    assert benchmark["candidate"]["source_sha256"] == validated["patched_file_sha256"]
    assert benchmark["correctness"]["equivalent"] is True
    assert benchmark["classification"] in {"IMPROVED", "INCONCLUSIVE", "NO_CLEAR_DIFFERENCE"}
    assert benchmark["speedup"] > 0

    final = client.get(f"/api/optimizations/{candidate_id}").json()
    assert final["state"] == "COMPLETED"
    assert final["benchmark_run_id"] == benchmark["id"]
    assert final["benchmark"]["classification"] == benchmark["classification"]
    assert _states(final) == [
        "PATCH_PROPOSED",
        "AWAITING_APPROVAL",
        "AWAITING_APPROVAL",
        "VALIDATING",
        "VALIDATED",
        "BENCHMARKING",
        "COMPLETED",
    ]
    again = client.post(
        "/api/benchmarks",
        json={"workload_id": workload["id"], "candidate": {"candidate_id": candidate_id}},
    )
    assert again.status_code == 409
    # The original workload tree was never modified.
    assert hash_tree(root)[0] == tree


def test_nested_loop_analysis_profiles_itself_and_completes(
    client: TestClient, settings: Settings
) -> None:
    workload = _workload(client, "nested_loop")
    root, tree = _registered_tree(client, "nested_loop")
    analysis = _analysis(client, settings, {"workload_id": workload["id"], "size": "small"})
    assert _states(analysis) == ["PROFILING", "PROFILED", "ANALYZING", "RECOMMENDED"]
    assert analysis["profile_requested"] is True
    profile = client.get(f"/api/profiles/{analysis['profile_run_id']}").json()
    assert profile["status"] == "SUCCEEDED"
    (rec,) = client.get(f"/api/analyses/{analysis['id']}/recommendations").json()
    assert rec["rule_id"] == "PP-LOOP-002"
    candidate = _candidate(client, rec["id"])
    assert candidate["transform"] == "nested_loop_join_to_index"
    client.post(f"/api/optimizations/{candidate['id']}/approve", json={})
    assert _validate(client, settings, candidate["id"])["state"] == "VALIDATED"
    accepted = client.post(
        "/api/benchmarks",
        json={
            "workload_id": workload["id"],
            "candidate": {"candidate_id": candidate["id"]},
            **SMALL_BENCHMARK,
        },
    )
    assert accepted.status_code == 202
    _drain(settings)
    final = client.get(f"/api/optimizations/{candidate['id']}").json()
    assert final["state"] == "COMPLETED"
    assert final["benchmark"]["status"] == "SUCCEEDED"
    assert hash_tree(root)[0] == tree


class _FixedProposer:
    """Stands in for an LLM proposer: returns a hand-written diff for the target."""

    name = "test-model"

    def __init__(self, path: str, transform: Any) -> None:
        self.path = path
        self.transform = transform

    def propose(self, request: PatchRequest) -> PatchProposal:
        root = request.target.source_root
        original = (root / self.path).read_text()
        patched = self.transform(original)
        return PatchProposal(
            status="proposed",
            rule_id=request.target.rule_id,
            target_file=request.target.target_file,
            target_function=request.target.target_function,
            generated_by=self.name,
            original_sha256=sha256_bytes((root / request.target.target_file).read_bytes()),
            diff=make_unified_diff(self.path, original, patched),
            summary="hand-written test proposal",
        )


@pytest.fixture
def custom_proposer() -> Iterator[Any]:
    def install(path: str, transform: Any) -> None:
        register_patch_proposer(lambda _settings: _FixedProposer(path, transform))

    yield install
    register_patch_proposer(None)


def _recommendation(client: TestClient, settings: Settings, slug: str) -> dict[str, Any]:
    workload = _workload(client, slug)
    analysis = _analysis(client, settings, {"workload_id": workload["id"], "size": "small"})
    rec: dict[str, Any] = client.get(f"/api/analyses/{analysis['id']}/recommendations").json()[0]
    return rec


def test_wrong_candidate_fails_validation_and_cannot_be_benchmarked(
    client: TestClient, settings: Settings, custom_proposer: Any
) -> None:
    rec = _recommendation(client, settings, "duplicate_counter")
    # Plausible but wrong: iterating a set changes the result's key order (first occurrence).
    custom_proposer(
        "workload.py",
        lambda text: text.replace("    for item in items:\n", "    for item in set(items):\n"),
    )
    candidate = _candidate(client, rec["id"])
    assert candidate["generated_by"] == "test-model"
    assert candidate["state"] == "AWAITING_APPROVAL"
    client.post(f"/api/optimizations/{candidate['id']}/approve", json={})
    failed = _validate(client, settings, candidate["id"])
    assert failed["state"] == "FAILED"
    assert failed["validation_status"] == "FAILED"
    checks = {c["name"]: c for c in failed["validation"]["checks"]}
    assert checks["functional_tests"]["status"] == "FAILED"
    assert any(
        "key_order" in f for f in checks["functional_tests"]["data"]["candidate"]["failures"]
    )
    workload = _workload(client, "duplicate_counter")
    blocked = client.post(
        "/api/benchmarks",
        json={"workload_id": workload["id"], "candidate": {"candidate_id": candidate["id"]}},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "invalid_state_transition"
    assert client.post(f"/api/optimizations/{candidate['id']}/validate").status_code == 409


def test_patches_touching_tests_are_rejected_by_the_validator(
    client: TestClient, settings: Settings, custom_proposer: Any
) -> None:
    rec = _recommendation(client, settings, "duplicate_counter")
    custom_proposer(
        "tests/test_workload.py", lambda text: text.replace("assert ", "assert True or ", 1)
    )
    candidate = _candidate(client, rec["id"])
    assert candidate["state"] == "REJECTED"
    assert candidate["approval"]["status"] == "REJECTED"
    assert candidate["approval"]["rejected_by"] == "patch-validator"
    codes = {r["code"] for r in candidate["rejection_reasons"]}
    assert {"protected_path", "not_allowed"} <= codes
    assert client.post(f"/api/optimizations/{candidate['id']}/approve", json={}).status_code == 409


def test_manual_rejection_is_final(client: TestClient, settings: Settings) -> None:
    rec = _recommendation(client, settings, "list_lookup")
    candidate = _candidate(client, rec["id"])
    rejected = client.post(
        f"/api/optimizations/{candidate['id']}/reject",
        json={"reason": "needs hashable ids we cannot guarantee", "rejected_by": "alice"},
    ).json()
    assert rejected["state"] == "REJECTED"
    assert rejected["rejection_reasons"][0]["reason"] == "needs hashable ids we cannot guarantee"
    assert rejected["approval"]["rejected_by"] == "alice"
    cid = candidate["id"]
    assert client.post(f"/api/optimizations/{cid}/approve", json={}).status_code == 409
    assert client.post(f"/api/optimizations/{cid}/validate").status_code == 409
    assert (
        client.post(f"/api/optimizations/{cid}/reject", json={"reason": "again"}).status_code == 409
    )
    workload = _workload(client, "list_lookup")
    response = client.post(
        "/api/benchmarks", json={"workload_id": workload["id"], "candidate": {"candidate_id": cid}}
    )
    assert response.status_code == 409


def test_no_safe_patch_is_reported(client: TestClient, settings: Settings) -> None:
    rec = _recommendation(client, settings, "string_builder")
    with session_scope() as session:
        original = session.get(OptimizationRecommendation, rec["id"])
        assert original is not None
        clone = OptimizationRecommendation(
            analysis_run_id=original.analysis_run_id,
            workload_id=original.workload_id,
            rank=2,
            rule_id="PP-LOOP-004",
            title="sort in loop",
            target_file=original.target_file,
            target_file_sha256=original.target_file_sha256,
            target_function=original.target_function,
            source_lines=original.source_lines,
            observed_bottleneck="x",
            suspected_root_cause="x",
            proposed_optimization="x",
            expected_effect="x",
            confidence="LOW",
        )
        session.add(clone)
        session.flush()
        clone_id = clone.id
    candidate = _candidate(client, clone_id)
    assert candidate["state"] == "FAILED"
    assert candidate["error"].startswith("no safe deterministic patch")
    assert client.get(f"/api/optimizations/{candidate['id']}/diff").status_code == 404
    assert client.post(f"/api/optimizations/{candidate['id']}/approve", json={}).status_code == 409


def test_analysis_request_errors(client: TestClient, settings: Settings) -> None:
    workload = _workload(client, "duplicate_counter")
    other = _workload(client, "list_lookup")
    with session_scope() as session:
        failed = ProfileRun(workload_id=workload["id"], size="small", status="FAILED")
        foreign = ProfileRun(workload_id=other["id"], size="small", status="SUCCEEDED")
        session.add_all([failed, foreign])
        session.flush()
        failed_id, foreign_id = failed.id, foreign.id
    response = client.post(
        "/api/analyses", json={"workload_id": workload["id"], "profile_id": failed_id}
    )
    assert response.status_code == 409
    assert "cannot provide hotspots" in response.json()["error"]["message"]
    assert (
        client.post(
            "/api/analyses", json={"workload_id": workload["id"], "profile_id": foreign_id}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/analyses", json={"workload_id": workload["id"], "profile_id": "nope"}
        ).status_code
        == 404
    )
    assert client.post("/api/analyses", json={"workload_id": "nope"}).status_code == 404
    assert (
        client.post(
            "/api/analyses", json={"workload_id": workload["id"], "size": "huge"}
        ).status_code
        == 422
    )
    for body in ({}, {"workload_id": "x", "extra": 1}):
        assert client.post("/api/analyses", json=body).status_code == 422
    for path in (
        "/api/analyses/nope",
        "/api/analyses/nope/recommendations",
        "/api/optimizations/nope",
        "/api/optimizations/nope/diff",
        "/api/optimizations/nope/validation",
    ):
        assert client.get(path).status_code == 404, path
    assert client.post("/api/optimizations", json={"recommendation_id": "nope"}).status_code == 404
    assert client.post("/api/optimizations/nope/approve", json={}).status_code == 404
    assert client.post("/api/optimizations/nope/reject", json={"reason": "x"}).status_code == 404
    assert client.post("/api/optimizations/nope/validate").status_code == 404
    assert (
        client.post("/api/optimizations/x/approve", json={"approved_by": "<script>"}).status_code
        == 422
    )
    assert client.post("/api/optimizations/x/reject", json={"reason": ""}).status_code == 422


def _benchmark_row(workload_id: str, candidate_id: str, **fields: Any) -> BenchmarkRun:
    return BenchmarkRun(
        workload_id=workload_id,
        size="medium",
        warmups=1,
        runs=5,
        memory_runs=0,
        threshold_percent=5.0,
        seed=1,
        order="random",
        bootstrap_resamples=1000,
        candidate_kind="candidate",
        candidate_id=candidate_id,
        candidate_label="test",
        **fields,
    )


def test_benchmark_outcomes_drive_the_candidate_state(
    client: TestClient, settings: Settings
) -> None:
    rec = _recommendation(client, settings, "regex_matching")
    candidate = _candidate(client, rec["id"])
    cid = candidate["id"]
    client.post(f"/api/optimizations/{cid}/approve", json={})
    assert _validate(client, settings, cid)["state"] == "VALIDATED"
    workload_id = candidate["workload_id"]
    with session_scope() as session:
        spec = build_spec(get_workload(session, workload_id))
        snapshot = create_snapshot(
            session,
            workload_id=workload_id,
            role="baseline",
            label="t",
            root=Path(spec.workspace_path),
        )
        assert candidate_resolver.check(session, spec, cid).startswith("validated patch")
        crashed = _benchmark_row(workload_id, cid, status="FAILED", error="worker died")
        session.add(crashed)
        session.flush()
        candidate_resolver.on_finished(session, crashed)
    state = client.get(f"/api/optimizations/{cid}").json()
    assert state["state"] == "VALIDATED"  # a crashed benchmark can be retried
    with session_scope() as session:
        candidate_resolver.check(session, spec, cid)
        different_input = _benchmark_row(
            workload_id,
            cid,
            status="SUCCEEDED",
            classification="IMPROVED",
            input_sha256="0" * 64,
            baseline_snapshot_id=snapshot.id,
        )
        session.add(different_input)
        session.flush()
        candidate_resolver.on_finished(session, different_input)
    final = client.get(f"/api/optimizations/{cid}").json()
    assert final["state"] == "FAILED"
    assert "not comparable" in final["error"]
