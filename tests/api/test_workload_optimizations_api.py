from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.jobs.worker import Worker


def _workload(client: TestClient, slug: str) -> dict[str, Any]:
    project = client.get("/api/projects").json()[0]
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    return next(w for w in workloads if w["slug"] == slug)


def _recommendation_id(client: TestClient, settings: Settings, workload_id: str) -> str:
    accepted = client.post("/api/analyses", json={"workload_id": workload_id, "size": "small"})
    assert accepted.status_code == 202, accepted.text
    Worker(settings, isolate=False).drain()
    recs = client.get(f"/api/analyses/{accepted.json()['subject_id']}/recommendations").json()
    rec_id: str = recs[0]["id"]
    return rec_id


def test_candidates_are_listed_per_workload_newest_first(
    client: TestClient, settings: Settings
) -> None:
    workload = _workload(client, "duplicate_counter")
    other = _workload(client, "list_lookup")
    url = f"/api/workloads/{workload['id']}/optimizations"
    assert client.get(url).json() == []

    rec_id = _recommendation_id(client, settings, workload["id"])
    first = client.post("/api/optimizations", json={"recommendation_id": rec_id}).json()
    second = client.post("/api/optimizations", json={"recommendation_id": rec_id}).json()

    listed = client.get(url).json()
    assert [c["id"] for c in listed] == [second["id"], first["id"]]
    assert listed[0]["state"] == "AWAITING_APPROVAL"
    assert client.get(url, params={"recommendation_id": "nope"}).json() == []
    assert len(client.get(url, params={"recommendation_id": rec_id, "limit": 1}).json()) == 1
    assert client.get(f"/api/workloads/{other['id']}/optimizations").json() == []


def test_unknown_workload_is_404(client: TestClient) -> None:
    response = client.get("/api/workloads/missing/optimizations")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
