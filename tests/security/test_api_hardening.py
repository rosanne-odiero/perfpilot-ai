"""API-level hardening: body limits, pagination caps, CORS, security headers, SSRF-shaped GitHub
URLs, tampered workspaces, and secrets in job errors / request logs."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingest_helpers import make_sample_repo
from log_support import capture_json_logs
from starlette.types import Message, Receive, Scope, Send

from backend.api.middleware import BodySizeLimitMiddleware, _limit_for
from backend.core.config import Settings
from backend.ingestion.workspace import get_workspace
from backend.jobs.registry import JobContext, job_handler
from backend.jobs.service import enqueue_job
from backend.jobs.worker import Worker
from backend.models.database import session_scope

LEAKED_TOKEN = "ghp_" + "Z" * 36


@job_handler("security_test.leak")
def _leaky_handler(ctx: JobContext) -> dict[str, Any]:
    raise RuntimeError(f"upstream said: Authorization token={LEAKED_TOKEN}")


@pytest.fixture
def small_body_client(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> TestClient:
    monkeypatch.setenv("PERFPILOT_MAX_REQUEST_BYTES", "2048")
    client: TestClient = request.getfixturevalue("client")
    return client


# ----------------------------------------------------------------------------- body size limits
def test_oversized_json_body_is_rejected_before_the_handler(small_body_client: TestClient) -> None:
    body = {"snapshot_id": "x" * 4096, "mode": "STATIC"}
    response = small_body_client.post("/api/audits", json=body)
    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "limit_exceeded"
    assert error["details"]["limit_bytes"] == 2048


def test_chunked_body_without_content_length_is_counted(small_body_client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"snapshot_id": "'
        for _ in range(8):
            yield b"y" * 512
        yield b'"}'

    response = small_body_client.post(
        "/api/audits", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "limit_exceeded"


def test_small_bodies_still_reach_validation(small_body_client: TestClient) -> None:
    response = small_body_client.post("/api/audits", json={"snapshot_id": "missing"})
    assert response.status_code in (404, 422)


def _run_asgi(headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> tuple[int, bool]:
    """Drive the middleware directly (lets us send headers no HTTP client would).

    Returns the response status and whether the app received the complete body."""
    reached: list[bool] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise OSError("client disconnected")
            if not message.get("more_body"):
                break
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "path": "/api/audits", "headers": headers, "method": "POST"}
    middleware = BodySizeLimitMiddleware(app, Settings(max_request_bytes=100))
    asyncio.run(middleware(scope, receive, send))
    return sent[0]["status"], bool(reached)


def test_invalid_content_length_is_rejected() -> None:
    assert _run_asgi([(b"content-length", b"12abc")], [b"{}"]) == (400, False)


def test_body_longer_than_declared_content_length_is_rejected() -> None:
    status, _ = _run_asgi([(b"content-length", b"10")], [b"x" * 60, b"x" * 60])
    assert status == 413


def test_streamed_body_without_length_never_reaches_the_app() -> None:
    assert _run_asgi([], [b"x" * 60, b"x" * 60]) == (413, False)


def test_upload_route_gets_the_upload_limit_not_the_json_limit() -> None:
    settings = Settings(max_request_bytes=100, max_upload_bytes=10_000)
    assert _limit_for("/api/repositories/upload", settings) > 10_000
    assert _limit_for("/api/audits", settings) == 100


# ----------------------------------------------------------------------------- pagination caps
@pytest.mark.parametrize(
    "url",
    [
        "/api/jobs?limit=501",
        "/api/jobs?limit=0",
        "/api/audits?limit=100000",
        "/api/repositories?limit=-1",
        "/api/audits/any/findings?limit=501",
        "/api/audits/any/findings?offset=-5",
    ],
)
def test_list_endpoints_enforce_limits(client: TestClient, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_error"


# ----------------------------------------------------------------------------- CORS & headers
def test_cors_allows_only_configured_origins(client: TestClient) -> None:
    preflight = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/api/audits", headers={"Origin": "http://localhost:8501", **preflight})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:8501"
    assert "access-control-allow-credentials" not in ok.headers
    evil = client.options("/api/audits", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in evil.headers
    simple = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_security_headers_on_api_responses(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    report = client.get("/api/reports/does-not-exist/download")
    assert report.status_code == 404
    assert report.headers["content-security-policy"].startswith("sandbox;")


# ----------------------------------------------------------------------------- SSRF-shaped URLs
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/o/r",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/o/r",
        "https://localhost/o/r",
        "https://0x7f000001/o/r",
        "https://github.com@127.0.0.1/o/r",
        "https://xn--gthub-zsa.com/o/r",
        "https://gíthub.com/o/r",
        "https://github.com%2f@evil.com/o/r",
        "https://github.com/o/r/../../../../etc/passwd",
        "https://github.com/" + "a" * 3000,
    ],
)
def test_github_import_rejects_ssrf_shaped_urls(client: TestClient, url: str) -> None:
    response = client.post("/api/repositories", json={"source_type": "GITHUB_PUBLIC", "url": url})
    assert 400 <= response.status_code < 500, response.text
    assert client.get("/api/jobs").json() == []


def test_unbounded_repository_fields_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/repositories",
        json={"source_type": "GITHUB_PUBLIC", "url": "https://github.com/o/r", "ref": "x" * 300},
    )
    assert response.status_code == 422


# ----------------------------------------------------------------------------- workspace tampering
def test_file_api_refuses_a_symlink_planted_in_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    repos = tmp_path / "repos"
    sample = make_sample_repo(repos / "sample")
    monkeypatch.setenv("PERFPILOT_LOCAL_ALLOWLIST", str(repos))
    client: TestClient = request.getfixturevalue("client")
    settings: Settings = request.getfixturevalue("settings")
    accepted = client.post(
        "/api/repositories", json={"source_type": "LOCAL_DIRECTORY", "local_path": str(sample)}
    ).json()
    Worker(settings, isolate=False).drain()
    snapshot_id = accepted["subject_id"]
    files = client.get(f"/api/snapshots/{snapshot_id}/files", params={"python_only": True})
    target_rel = files.json()["files"][0]["path"]

    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP-SECRET-OUTSIDE-WORKSPACE")
    planted = get_workspace(settings, snapshot_id).src / target_rel
    os.chmod(planted.parent, 0o755)
    planted.unlink()
    planted.symlink_to(secret)

    response = client.get(
        f"/api/snapshots/{snapshot_id}/files/content", params={"path": target_rel}
    )
    assert response.status_code in (400, 404, 409)
    assert "TOP-SECRET" not in response.text


@pytest.mark.parametrize("path", ["%2e%2e/%2e%2e/etc/passwd", "app/\x00main.py", "C:\\Windows\\x"])
def test_file_api_rejects_encoded_and_odd_paths(client: TestClient, path: str) -> None:
    response = client.get("/api/snapshots/unknown/files/content", params={"path": path})
    assert 400 <= response.status_code < 500


# ----------------------------------------------------------------------------- secrets in outputs
def test_job_errors_are_redacted_before_persisting(client: TestClient, settings: Settings) -> None:
    with session_scope() as session:
        job_id = enqueue_job(session, "security_test.leak", {}).id
    with capture_json_logs() as logs:
        Worker(settings, isolate=False).drain()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "FAILED"
    assert LEAKED_TOKEN not in job["error"]
    assert LEAKED_TOKEN not in str(job["error_details"])
    assert "[REDACTED]" in job["error"]
    assert LEAKED_TOKEN not in logs.text()


def test_request_logs_do_not_include_query_strings(client: TestClient) -> None:
    with capture_json_logs() as logs:
        client.get("/api/jobs", params={"job_type": LEAKED_TOKEN})
    [record] = [r for r in logs.named("request") if r["path"] == "/api/jobs"]
    assert record["status"] == 200
    assert LEAKED_TOKEN not in logs.text()
