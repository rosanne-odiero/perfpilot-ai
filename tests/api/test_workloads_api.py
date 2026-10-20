from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.infrastructure.artifact_store import ArtifactStore
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.orm.platform import Artifact
from backend.models.workloads import REFERENCE_CANDIDATE_LABEL


def _project(client: TestClient) -> dict[str, Any]:
    projects = client.get("/api/projects").json()
    assert [p["name"] for p in projects] == ["Bundled examples"]
    return projects[0]


def _workload(client: TestClient, slug: str) -> dict[str, Any]:
    project = _project(client)
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    return next(w for w in workloads if w["slug"] == slug)


def test_projects_and_workloads_are_seeded_idempotently(client: TestClient) -> None:
    project = _project(client)
    assert project["workload_count"] == 5
    assert client.get(f"/api/projects/{project['id']}").json()["id"] == project["id"]
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    assert [w["slug"] for w in workloads] == [
        "duplicate_counter",
        "list_lookup",
        "nested_loop",
        "regex_matching",
        "string_builder",
    ]
    from backend.services.workloads import seed_bundled_examples

    with session_scope() as session:
        seed_bundled_examples(session)
    assert _project(client)["workload_count"] == 5


def test_workload_detail_includes_source_and_fixture(client: TestClient) -> None:
    workload = _workload(client, "duplicate_counter")
    detail = client.get(f"/api/workloads/{workload['id']}").json()
    assert detail["entrypoint"] == "workload:count_duplicates"
    assert "def count_duplicates" in detail["entrypoint_source"]["content"]
    assert detail["entrypoint_source"]["path"] == "workload.py"
    assert "def make_input" in detail["fixture_source"]["content"]
    assert "Zipf" in detail["fixture_description"]
    assert detail["size_parameters"]["medium"] == {"n_items": 4000}
    assert detail["has_reference_candidate"] is True
    assert "workspace_path" not in detail["spec"]
    assert detail["readme"].startswith("# Duplicate counter")


def test_unknown_ids_return_404(client: TestClient) -> None:
    for path in (
        "/api/projects/nope",
        "/api/projects/nope/workloads",
        "/api/workloads/nope",
        "/api/profiles/nope",
        "/api/profiles/nope/hotspots",
        "/api/benchmarks/nope",
        "/api/benchmarks/nope/results",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"
    assert client.post("/api/profiles", json={"workload_id": "nope"}).status_code == 404
    assert client.post("/api/benchmarks", json={"workload_id": "nope"}).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"workload_id": "x", "unexpected": 1},
        {"workload_id": "x", "enable_sampling": "maybe"},
    ],
)
def test_profile_request_validation(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/api/profiles", json=body).status_code == 422


def test_unknown_size_is_rejected(client: TestClient) -> None:
    workload = _workload(client, "list_lookup")
    response = client.post("/api/profiles", json={"workload_id": workload["id"], "size": "huge"})
    assert response.status_code == 422
    assert response.json()["error"]["details"]["allowed"] == ["large", "medium", "small"]


@pytest.mark.parametrize(
    "overrides",
    [{"runs": 1}, {"warmups": -1}, {"threshold_percent": 0}, {"candidate": "mine"}, {"seed": -5}],
)
def test_benchmark_request_validation(client: TestClient, overrides: dict[str, Any]) -> None:
    workload = _workload(client, "list_lookup")
    response = client.post("/api/benchmarks", json={"workload_id": workload["id"], **overrides})
    assert response.status_code == 422


def test_candidate_id_requires_a_registered_resolver(client: TestClient) -> None:
    workload = _workload(client, "list_lookup")
    response = client.post(
        "/api/benchmarks",
        json={"workload_id": workload["id"], "candidate": {"candidate_id": "c-1"}},
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_configured"


def test_profile_end_to_end(client: TestClient, settings: Settings) -> None:
    workload = _workload(client, "duplicate_counter")
    accepted = client.post(
        "/api/profiles",
        json={"workload_id": workload["id"], "size": "small", "enable_sampling": True},
    )
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["subject_type"] == "profile_run"
    profile_id = body["subject_id"]
    assert client.get(f"/api/profiles/{profile_id}").json()["status"] == "PENDING"

    Worker(settings, isolate=False).drain()
    assert client.get(f"/api/jobs/{body['job_id']}").json()["status"] == "SUCCEEDED"
    profile = client.get(f"/api/profiles/{profile_id}").json()
    assert profile["status"] == "SUCCEEDED"
    assert profile["provenance"] == "MEASURED"
    assert profile["total_profiled_time_ms"] > 0
    assert profile["top_user_hotspots"][0]["function_name"] == "count_duplicates"
    assert profile["top_user_hotspots"][0]["file_path"] == "workload.py"
    assert profile["memory"]["tracemalloc_peak_bytes"] > 0
    assert profile["memory"]["rss_peak_bytes"] > profile["memory"]["tracemalloc_peak_bytes"]
    assert profile["sampling"]["requested"] is True
    if not profile["sampling"]["available"]:
        assert any("sampling profiler skipped" in w for w in profile["warnings"])
    assert profile["environment"]["workload"] == "duplicate_counter"
    assert profile["source_snapshot"]["files"]["workload.py"]

    hotspots = client.get(f"/api/profiles/{profile_id}/hotspots").json()
    assert sum(h["percentage_of_profiled_time"] for h in hotspots) == pytest.approx(100.0)
    by_calls = client.get(f"/api/profiles/{profile_id}/hotspots", params={"sort": "calls"}).json()
    assert by_calls[0]["total_calls"] == max(h["total_calls"] for h in hotspots)
    user = client.get(f"/api/profiles/{profile_id}/hotspots", params={"scope": "user"}).json()
    assert all(h["is_user_code"] for h in user)
    assert (
        client.get(f"/api/profiles/{profile_id}/hotspots", params={"sort": "bogus"}).status_code
        == 422
    )

    with session_scope() as session:
        artifact = session.get(Artifact, profile["artifacts"]["prof"])
        assert artifact is not None
        assert ArtifactStore(settings).path_for(artifact).stat().st_size == artifact.size_bytes


def test_benchmark_end_to_end(client: TestClient, settings: Settings) -> None:
    workload = _workload(client, "list_lookup")
    accepted = client.post(
        "/api/benchmarks",
        json={
            "workload_id": workload["id"],
            "size": "small",
            "warmups": 1,
            "runs": 5,
            "memory_runs": 1,
            "bootstrap_resamples": 1000,
        },
    )
    assert accepted.status_code == 202
    benchmark_id = accepted.json()["subject_id"]
    Worker(settings, isolate=False).drain()
    result = client.get(f"/api/benchmarks/{benchmark_id}").json()
    assert result["status"] == "SUCCEEDED", result["error"]
    assert result["candidate"]["label"] == REFERENCE_CANDIDATE_LABEL
    assert result["candidate"]["kind"] == "reference"
    assert result["candidate"]["candidate_id"] is None
    assert result["candidate"]["source_sha256"]
    assert result["baseline_snapshot"]["tree_sha256"] != result["candidate_snapshot"]["tree_sha256"]
    assert result["classification"] in {"IMPROVED", "INCONCLUSIVE"}
    assert result["speedup"] > 1
    assert result["speedup_ci_low"] <= result["speedup"] <= result["speedup_ci_high"]
    summary = result["summary"]
    assert summary["baseline"]["wall_ms"]["n"] == 5
    assert summary["latency_reduction_percent"] == pytest.approx(
        (summary["baseline"]["wall_ms"]["median"] - summary["candidate"]["wall_ms"]["median"])
        / summary["baseline"]["wall_ms"]["median"]
        * 100
    )
    assert result["memory"]["baseline"]["tracemalloc_peak_bytes_median"] is not None
    assert result["environment"]["settings"]["warmups"] == 1
    assert result["environment"]["candidate_source_sha256"] == result["candidate"]["source_sha256"]
    assert result["correctness"]["equivalent"] is True

    samples = client.get(f"/api/benchmarks/{benchmark_id}/results").json()
    timing = [s for s in samples if s["kind"] == "timing"]
    assert len(timing) == 10
    assert all(s["input_sha256"] == result["input_sha256"] for s in samples)
    only_memory = client.get(
        f"/api/benchmarks/{benchmark_id}/results", params={"kind": "memory"}
    ).json()
    assert {s["kind"] for s in only_memory} == {"memory"}
