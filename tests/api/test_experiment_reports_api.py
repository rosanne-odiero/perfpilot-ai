"""API tests for experiment reports and the experiment history."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.jobs.worker import Worker

SMALL_BENCHMARK = {
    "size": "small",
    "warmups": 0,
    "runs": 3,
    "memory_runs": 0,
    "bootstrap_resamples": 1000,
}


def _drain(settings: Settings) -> None:
    Worker(settings, isolate=False).drain()


def _workload(client: TestClient, slug: str) -> dict[str, Any]:
    project = client.get("/api/projects").json()[0]
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    return next(w for w in workloads if w["slug"] == slug)


def _candidate(client: TestClient, settings: Settings, slug: str) -> dict[str, Any]:
    workload = _workload(client, slug)
    accepted = client.post("/api/analyses", json={"workload_id": workload["id"], "size": "small"})
    assert accepted.status_code == 202, accepted.text
    _drain(settings)
    recs = client.get(f"/api/analyses/{accepted.json()['subject_id']}/recommendations").json()
    response = client.post("/api/optimizations", json={"recommendation_id": recs[0]["id"]})
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _error_code(response: Any) -> str:
    code: str = response.json()["error"]["code"]
    return code


def test_unknown_subjects_and_bad_requests(client: TestClient) -> None:
    for path in ("/api/optimizations/nope/reports", "/api/benchmarks/nope/reports"):
        assert client.post(path, json={"formats": ["md"]}).status_code == 404
        assert client.get(path).status_code == 404
    assert (
        client.post("/api/optimizations/x/reports", json={"formats": ["docx"]}).status_code == 422
    )
    assert client.post("/api/optimizations/x/reports", json={"formats": []}).status_code == 422
    assert client.get("/api/experiments", params={"limit": 0}).status_code == 422
    assert client.get("/api/experiments", params={"kind": "audit"}).status_code == 422
    assert client.get("/api/experiments").json() == []


def test_awaiting_approval_report_is_honest_and_busy_candidates_are_refused(
    client: TestClient, settings: Settings
) -> None:
    candidate = _candidate(client, settings, "duplicate_counter")
    accepted = client.post(
        f"/api/optimizations/{candidate['id']}/reports", json={"formats": ["json", "json"]}
    )
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["subject_type"] == "optimization"
    assert len(body["reports"]) == 1
    _drain(settings)
    report_id = body["reports"][0]["report_id"]
    meta = client.get(f"/api/reports/{report_id}").json()
    assert meta["status"] == "SUCCEEDED"
    assert meta["audit_id"] is None
    assert meta["subject_id"] == candidate["id"]
    assert meta["meta"]["experiment_state"] == "AWAITING_APPROVAL"
    report = json.loads(client.get(f"/api/reports/{report_id}/download").content)
    assert report["outcome"]["state"] == "AWAITING_APPROVAL"
    assert report["validation"] is None
    assert report["benchmark"] is None
    assert "explicit approval" in report["validation_absent_reason"]
    assert report["approval"]["status"] == "PENDING"
    assert client.get(f"/api/reports/{report_id}/preview").status_code == 422

    # Approval changes the candidate version, so a new report is generated.
    client.post(f"/api/optimizations/{candidate['id']}/approve", json={})
    again = client.post(f"/api/optimizations/{candidate['id']}/reports", json={"formats": ["json"]})
    assert again.json()["reports"][0]["report_id"] != report_id

    assert client.post(f"/api/optimizations/{candidate['id']}/validate").status_code == 202
    busy = client.post(f"/api/optimizations/{candidate['id']}/reports", json={"formats": ["md"]})
    assert busy.status_code == 409
    assert busy.json()["error"]["details"]["state"] == "VALIDATING"
    _drain(settings)
    listed = client.get(f"/api/optimizations/{candidate['id']}/reports").json()
    assert len(listed) == 2
    assert all(r["subject_type"] == "optimization" for r in listed)


def test_reference_benchmark_report_and_history(client: TestClient, settings: Settings) -> None:
    workload = _workload(client, "nested_loop")
    accepted = client.post(
        "/api/benchmarks", json={"workload_id": workload["id"], **SMALL_BENCHMARK}
    )
    benchmark_id = accepted.json()["subject_id"]
    pending = client.post(f"/api/benchmarks/{benchmark_id}/reports", json={"formats": ["md"]})
    assert pending.status_code == 409
    assert _error_code(pending) == "conflict"
    _drain(settings)
    assert client.get(f"/api/benchmarks/{benchmark_id}").json()["status"] == "SUCCEEDED"

    accepted = client.post(
        f"/api/benchmarks/{benchmark_id}/reports", json={"formats": ["json", "md", "csv", "html"]}
    )
    assert accepted.status_code == 202
    assert accepted.json()["subject_type"] == "benchmark"
    _drain(settings)
    listed = {r["format"]: r for r in client.get(f"/api/benchmarks/{benchmark_id}/reports").json()}
    assert set(listed) == {"json", "md", "csv", "html"}
    page = client.get(f"/api/reports/{listed['html']['id']}/preview").text
    assert "Not applicable: this experiment benchmarks the bundled reference" in page
    assert "<svg" in page
    report = json.loads(client.get(f"/api/reports/{listed['json']['id']}/download").content)
    assert report["kind"] == "reference_benchmark"
    assert report["recommendation"] is None
    assert report["patch"] is None
    assert report["benchmark"]["candidate_kind"] == "reference"
    assert report["benchmark"]["memory"] is not None
    assert report["benchmark"]["settings"]["runs"] == 3
    assert "hand-written" in report["summary_box"]["verdict"]
    assert any(
        "run_workload.py benchmark nested_loop" in c for c in report["reproducibility"]["cli"]
    )
    markdown = client.get(f"/api/reports/{listed['md']['id']}/preview")
    assert markdown.status_code == 200
    assert "Reference benchmark" in markdown.text
    samples = client.get(f"/api/reports/{listed['csv']['id']}/download").text
    assert samples.count("\ntiming") == 0
    assert samples.count(",timing,") == 6
    checks = client.get(
        f"/api/reports/{listed['csv']['id']}/download", params={"part": "validation_checks"}
    )
    assert checks.status_code == 200
    assert checks.text.count("\n") == 1

    history = client.get("/api/experiments", params={"kind": "reference_benchmark"}).json()
    assert [e["id"] for e in history] == [benchmark_id]
    entry = history[0]
    assert entry["subject_type"] == "benchmark"
    assert entry["report_count"] == 4
    assert entry["speedup"] == report["benchmark"]["speedup"]
    assert entry["median_baseline_ms"] is not None
    filtered = client.get("/api/experiments", params={"workload_id": "other"}).json()
    assert filtered == []


def test_candidate_benchmarks_point_to_the_candidate_report(
    client: TestClient, settings: Settings
) -> None:
    candidate = _candidate(client, settings, "duplicate_counter")
    client.post(f"/api/optimizations/{candidate['id']}/approve", json={})
    client.post(f"/api/optimizations/{candidate['id']}/validate")
    _drain(settings)
    accepted = client.post(
        "/api/benchmarks",
        json={
            "workload_id": candidate["workload_id"],
            "candidate": {"candidate_id": candidate["id"]},
            **SMALL_BENCHMARK,
        },
    )
    assert accepted.status_code == 202, accepted.text
    _drain(settings)
    refused = client.post(
        f"/api/benchmarks/{accepted.json()['subject_id']}/reports", json={"formats": ["md"]}
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["candidate_id"] == candidate["id"]
    history = client.get("/api/experiments").json()
    assert [e["kind"] for e in history] == ["optimization"]
    assert history[0]["state"] == "COMPLETED"
    assert history[0]["benchmark_id"] == accepted.json()["subject_id"]
