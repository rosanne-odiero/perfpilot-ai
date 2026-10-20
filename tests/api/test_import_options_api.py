from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings, reset_settings_cache


def test_import_options_without_allowlist(client: TestClient, settings: Settings) -> None:
    body = client.get("/api/import/options").json()
    assert body["local_directory_enabled"] is False
    assert body["local_allowlist_roots"] == 0
    assert body["github_enabled"] is True
    assert body["github_allowed_hosts"] == ["github.com"]
    assert body["limits"]["max_upload_bytes"] == settings.max_upload_bytes
    assert body["limits"]["max_python_files"] == settings.max_python_files


def test_import_options_reports_allowlist_count_but_never_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    secret_root = tmp_path / "private-projects"
    secret_root.mkdir()
    monkeypatch.setenv("PERFPILOT_LOCAL_ALLOWLIST", f"{secret_root},{tmp_path}")
    monkeypatch.setenv("PERFPILOT_MAX_UPLOAD_BYTES", "1234")
    reset_settings_cache()
    from backend.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/api/import/options")
    assert response.status_code == 200
    body = response.json()
    assert body["local_directory_enabled"] is True
    assert body["local_allowlist_roots"] == 2
    assert body["limits"]["max_upload_bytes"] == 1234
    assert str(tmp_path) not in response.text
