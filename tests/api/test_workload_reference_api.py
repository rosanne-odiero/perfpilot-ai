from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def _workload(client: TestClient, slug: str) -> dict[str, Any]:
    project = client.get("/api/projects").json()[0]
    workloads = client.get(f"/api/projects/{project['id']}/workloads").json()
    return next(w for w in workloads if w["slug"] == slug)


def test_reference_source_is_labeled_and_differs_from_baseline(client: TestClient) -> None:
    workload = _workload(client, "duplicate_counter")
    response = client.get(f"/api/workloads/{workload['id']}/reference")
    assert response.status_code == 200
    body = response.json()
    assert body["label"] == "reference implementation (bundled, for testing)"
    assert body["baseline"]["path"] == "workload.py"
    assert body["reference"]["path"] == "reference/optimized.py"
    assert "def count_duplicates" in body["baseline"]["content"]
    assert "Counter" in body["reference"]["content"]
    assert body["baseline"]["content"] != body["reference"]["content"]
    assert body["reference"]["truncated"] is False


def test_reference_source_unknown_workload_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/workloads/does-not-exist/reference")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
