from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from ingest_helpers import FAKE_OPENAI_KEY, make_sample_repo, zip_tree

from backend.core.config import Settings
from backend.jobs.worker import Worker


@pytest.fixture
def repos_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("PERFPILOT_LOCAL_ALLOWLIST", str(root))
    monkeypatch.setenv("PERFPILOT_MAX_UPLOAD_BYTES", str(256 * 1024))
    return root


@pytest.fixture
def api(repos_dir: Path, client: TestClient) -> TestClient:
    return client


@pytest.fixture
def sample(repos_dir: Path) -> Path:
    return make_sample_repo(repos_dir / "sample")


def _drain(settings: Settings) -> None:
    Worker(settings, isolate=False).drain()


def _import_local(api: TestClient, path: Path, **extra: Any) -> dict[str, Any]:
    response = api.post(
        "/api/repositories",
        json={"source_type": "LOCAL_DIRECTORY", "local_path": str(path), **extra},
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def _upload(api: TestClient, data: bytes, filename: str = "sample.zip", **fields: str) -> Any:
    return api.post(
        "/api/repositories/upload",
        files={"file": (filename, data, "application/zip")},
        data=fields,
    )


def _ready_snapshot(api: TestClient, settings: Settings, accepted: dict[str, Any]) -> str:
    _drain(settings)
    job = api.get(f"/api/jobs/{accepted['job_id']}").json()
    assert job["status"] == "SUCCEEDED", job
    snapshot_id: str = accepted["subject_id"]
    assert api.get(f"/api/snapshots/{snapshot_id}").json()["status"] == "READY"
    return snapshot_id


def test_local_directory_import_end_to_end(
    api: TestClient, settings: Settings, sample: Path
) -> None:
    accepted = _import_local(api, sample)
    assert accepted["status"] == "QUEUED"
    assert accepted["subject_type"] == "snapshot"
    snapshot_id = _ready_snapshot(api, settings, accepted)

    snapshot = api.get(f"/api/snapshots/{snapshot_id}").json()
    assert snapshot["counts"]["python_files"] == 4
    assert snapshot["counts"]["skipped_by_reason"]["sensitive"] == 2
    assert snapshot["root_name"] == "sample"
    assert snapshot["content_hash"]
    assert snapshot["limits_hit"] == []
    assert {"FastAPI", "Next.js", "Docker"} <= {t["name"] for t in snapshot["technologies"]}

    repositories = api.get("/api/repositories").json()
    assert len(repositories) == 1
    repo = repositories[0]
    assert repo["source_type"] == "LOCAL_DIRECTORY"
    assert repo["latest_snapshot_status"] == "READY"

    detail = api.get(f"/api/repositories/{repo['id']}").json()
    latest = detail["latest_snapshot"]
    assert latest["id"] == snapshot_id
    assert any(d["name"] == "fastapi" for d in latest["dependencies"])
    assert latest["git"]["commit_sha"] is None


def test_file_listing_filters(api: TestClient, settings: Settings, sample: Path) -> None:
    snapshot_id = _ready_snapshot(api, settings, _import_local(api, sample))
    url = f"/api/snapshots/{snapshot_id}/files"

    everything = api.get(url).json()
    paths = {f["path"]: f for f in everything["files"]}
    assert paths[".env"]["skip_reason"] == "sensitive"
    assert paths["node_modules/"]["skip_reason"] == "node_modules"
    assert everything["total"] == len(everything["files"])

    kept = api.get(url, params={"include_skipped": False}).json()["files"]
    assert all(not f["skipped"] for f in kept)

    python = api.get(url, params={"python_only": True, "include_skipped": False}).json()
    assert {f["path"] for f in python["files"]} == {
        "app/__init__.py",
        "app/main.py",
        "app/models.py",
        "migrations/0001_initial.py",
    }
    js = api.get(url, params={"language": "javascript"}).json()["files"]
    assert {f["path"] for f in js} == {"static/app.min.js", "static/bundle.js"}


def test_file_content_is_redacted(api: TestClient, settings: Settings, sample: Path) -> None:
    snapshot_id = _ready_snapshot(api, settings, _import_local(api, sample))
    response = api.get(
        f"/api/snapshots/{snapshot_id}/files/content", params={"path": "app/main.py"}
    )
    assert response.status_code == 200
    body = response.json()
    assert FAKE_OPENAI_KEY not in body["content"]
    assert "[REDACTED]" in body["content"]
    assert body["redactions"] >= 1
    assert body["language"] == "Python"
    assert "from fastapi import FastAPI" in body["content"]


@pytest.mark.parametrize("path", [".env", "config/secrets.json", "backend/.env", "x/id_rsa"])
def test_sensitive_file_content_is_forbidden(
    api: TestClient, settings: Settings, sample: Path, path: str
) -> None:
    snapshot_id = _ready_snapshot(api, settings, _import_local(api, sample))
    response = api.get(f"/api/snapshots/{snapshot_id}/files/content", params={"path": path})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "sensitive_file"
    assert "hunter2" not in response.text


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("assets/logo.png", 422, "validation_failed"),
        ("node_modules/pkg/index.js", 404, "not_found"),
        ("app/missing.py", 404, "not_found"),
        ("../sample/app/main.py", 400, "security_violation"),
        ("/etc/passwd", 400, "security_violation"),
    ],
)
def test_file_content_rejections(
    api: TestClient, settings: Settings, sample: Path, path: str, status: int, code: str
) -> None:
    snapshot_id = _ready_snapshot(api, settings, _import_local(api, sample))
    response = api.get(f"/api/snapshots/{snapshot_id}/files/content", params={"path": path})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_zip_upload_end_to_end(
    api: TestClient, settings: Settings, sample: Path, tmp_path: Path
) -> None:
    archive = zip_tree(sample, tmp_path / "sample.zip", "sample-main/")
    response = _upload(api, archive.read_bytes(), display_name="Sample Upload")
    assert response.status_code == 202, response.text
    accepted = response.json()
    snapshot_id = _ready_snapshot(api, settings, accepted)
    snapshot = api.get(f"/api/snapshots/{snapshot_id}").json()
    assert snapshot["root_name"] == "Sample Upload"
    assert snapshot["counts"]["python_files"] == 4
    repo = api.get(f"/api/repositories/{snapshot['repository_id']}").json()
    assert repo["source_type"] == "ZIP_UPLOAD"
    assert repo["name"] == "Sample Upload"
    assert repo["source_locator"].startswith("zip:sha256:")

    local = _ready_snapshot(api, settings, _import_local(api, sample))
    local_files = api.get(f"/api/snapshots/{local}/files").json()["files"]
    zip_files = api.get(f"/api/snapshots/{snapshot_id}/files").json()["files"]
    assert local_files == zip_files
    assert not list((settings.resolved_data_dir / "uploads").iterdir())


def test_duplicate_requests_reuse_the_in_flight_snapshot(
    api: TestClient, settings: Settings, sample: Path, tmp_path: Path
) -> None:
    first = _import_local(api, sample)
    second = _import_local(api, sample)
    assert first == second
    archive = zip_tree(sample, tmp_path / "s.zip").read_bytes()
    up1 = _upload(api, archive).json()
    up2 = _upload(api, archive).json()
    assert up1 == up2
    _drain(settings)
    third = _import_local(api, sample)
    assert third["subject_id"] != first["subject_id"]
    assert len(api.get("/api/repositories").json()) == 2


@pytest.mark.parametrize("kib", [512, 300])
def test_upload_size_limit(api: TestClient, settings: Settings, kib: int) -> None:
    """512 KiB is refused from Content-Length; 300 KiB is within the multipart allowance and is
    stopped by the streaming byte counter instead."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("big.bin", bytes(range(256)) * 4 * kib)
    response = _upload(api, buffer.getvalue())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "limit_exceeded"
    staging = settings.resolved_data_dir / "uploads"
    assert not staging.exists() or not list(staging.iterdir())


@pytest.mark.parametrize(
    ("data", "filename"),
    [(b"not a zip at all", "x.zip"), (b"PK\x03\x04", "x.tar.gz"), (b"", "empty.zip")],
)
def test_upload_rejects_non_zip(api: TestClient, data: bytes, filename: str) -> None:
    response = _upload(api, data, filename=filename)
    assert response.status_code == 422


def test_upload_requires_multipart_file(api: TestClient) -> None:
    assert api.post("/api/repositories/upload", content=b"PK").status_code == 422
    no_file = api.post(
        "/api/repositories/upload", data={"display_name": "x"}, files={"other": ("a.txt", b"x")}
    )
    assert no_file.status_code == 422


def test_unsafe_zip_fails_the_job(api: TestClient, settings: Settings, tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("ok.py", "x = 1\n")
        zf.writestr("../../evil.py", "pwned\n")
    accepted = _upload(api, buffer.getvalue()).json()
    _drain(settings)
    job = api.get(f"/api/jobs/{accepted['job_id']}").json()
    assert job["status"] == "FAILED"
    assert job["error_details"]["code"] == "unsafe_archive"
    snapshot = api.get(f"/api/snapshots/{accepted['subject_id']}").json()
    assert snapshot["status"] == "FAILED"
    assert "escapes" in snapshot["error"]
    assert not (settings.workspaces_dir / accepted["subject_id"]).exists()
    files = api.get(f"/api/snapshots/{accepted['subject_id']}/files")
    assert files.status_code == 409


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"source_type": "GITHUB_PUBLIC"}, 422, "request_validation_error"),
        ({"source_type": "NOPE", "url": "x"}, 422, "request_validation_error"),
        (
            {"source_type": "GITHUB_PUBLIC", "url": "ssh://git@github.com/o/r"},
            400,
            "security_violation",
        ),
        ({"source_type": "GITHUB_PUBLIC", "url": "file:///etc"}, 400, "security_violation"),
        (
            {"source_type": "GITHUB_PUBLIC", "url": "https://t:x@github.com/o/r"},
            400,
            "security_violation",
        ),
        (
            {"source_type": "GITHUB_PUBLIC", "url": "https://gitlab.com/o/r"},
            400,
            "security_violation",
        ),
        ({"source_type": "GITHUB_PUBLIC", "url": "https://github.com/o"}, 422, "validation_failed"),
        ({"source_type": "GITHUB_PRIVATE", "url": "https://github.com/o/r"}, 501, "not_configured"),
        ({"source_type": "ZIP_UPLOAD", "upload_artifact_id": "x"}, 422, "validation_failed"),
        ({"source_type": "LOCAL_DIRECTORY", "local_path": "/etc"}, 400, "security_violation"),
        ({"source_type": "LOCAL_DIRECTORY", "local_path": "relative/x"}, 422, "validation_failed"),
    ],
)
def test_import_rejections(
    api: TestClient, payload: dict[str, str], status: int, code: str
) -> None:
    response = api.post("/api/repositories", json=payload)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_local_path_traversal_is_rejected(api: TestClient, repos_dir: Path) -> None:
    response = api.post(
        "/api/repositories",
        json={"source_type": "LOCAL_DIRECTORY", "local_path": f"{repos_dir}/../"},
    )
    assert response.status_code == 400
    missing = api.post(
        "/api/repositories",
        json={"source_type": "LOCAL_DIRECTORY", "local_path": f"{repos_dir}/missing"},
    )
    assert missing.status_code == 404


def test_github_import_is_accepted_without_network(api: TestClient) -> None:
    response = api.post(
        "/api/repositories",
        json={"source_type": "GITHUB_PUBLIC", "url": "https://github.com/octocat/Hello-World"},
    )
    assert response.status_code == 202
    repos = api.get("/api/repositories").json()
    assert repos[0]["name"] == "octocat/Hello-World"
    assert repos[0]["source_locator"] == "https://github.com/octocat/Hello-World"
    assert repos[0]["latest_snapshot_status"] == "PENDING"


def test_unknown_ids_return_404(api: TestClient) -> None:
    for url in (
        "/api/repositories/nope",
        "/api/snapshots/nope",
        "/api/snapshots/nope/files",
        "/api/snapshots/nope/files/content?path=a.py",
    ):
        response = api.get(url)
        assert response.status_code == 404, url
        assert response.json()["error"]["code"] == "not_found"


def test_content_requires_path(api: TestClient) -> None:
    assert api.get("/api/snapshots/x/files/content").status_code == 422
