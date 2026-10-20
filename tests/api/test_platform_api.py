from __future__ import annotations

from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.jobs import service
from backend.jobs.worker import Worker
from backend.models.database import session_scope


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["llm_configured"] is False
    assert "system.echo" in body["job_types"]


def test_job_lifecycle_via_api(client: TestClient, settings: Settings) -> None:
    with session_scope() as session:
        job_id = service.enqueue_job(session, "system.echo", {"message": "api"}).id
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "QUEUED"
    Worker(settings, isolate=False).drain()
    body = client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "SUCCEEDED"
    assert body["result"] == {"echo": "api"}
    assert any(j["id"] == job_id for j in client.get("/api/jobs").json())


def test_unknown_job_returns_error_envelope(client: TestClient) -> None:
    response = client.get("/api/jobs/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_validation_error_envelope(client: TestClient) -> None:
    response = client.get("/api/jobs", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


def test_openapi_available(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
