from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.config import Settings
from backend.core.exceptions import NotFoundError
from backend.core.logging import scrub
from backend.infrastructure.artifact_store import ArtifactStore, safe_filename, sha256_bytes
from backend.models.audit_schemas import CodeLocation, RepositoryRef
from backend.models.database import session_scope
from backend.models.enums import RepositorySourceType


def test_artifact_roundtrip(settings: Settings) -> None:
    store = ArtifactStore(settings)
    with session_scope() as session:
        art = store.save_bytes(session, kind="report", filename="../../evil name.md", data=b"hi")
        assert art.sha256 == sha256_bytes(b"hi")
        assert ".." not in art.relative_path
        assert store.read_bytes(art) == b"hi"
        assert store.path_for(art).is_relative_to(settings.artifacts_dir)
        with pytest.raises(NotFoundError):
            store.get(session, "missing")


def test_safe_filename() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("") == "artifact"


def test_settings_parse_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PERFPILOT_LOCAL_ALLOWLIST", f"{tmp_path}/a, {tmp_path}/b")
    cfg = Settings()
    assert cfg.local_allowlist == [tmp_path / "a", tmp_path / "b"]
    assert not cfg.llm_configured


def test_scrub_removes_secrets() -> None:
    text = "api_key=sk-abcdefghijklmnopqrstuv token: ghp_" + "a" * 30
    cleaned = scrub(text)
    assert "sk-abc" not in cleaned
    assert "ghp_" not in cleaned
    assert scrub("https://user:pass@github.com/x") == "[REDACTED]github.com/x"


def test_repository_ref_requires_locator() -> None:
    with pytest.raises(ValueError, match="missing locator"):
        RepositoryRef(source_type=RepositorySourceType.GITHUB_PUBLIC)
    ref = RepositoryRef(source_type=RepositorySourceType.LOCAL_DIRECTORY, local_path="/x")
    assert ref.local_path == "/x"


def test_code_location_ordering() -> None:
    with pytest.raises(ValueError, match="line_end"):
        CodeLocation(file_path="a.py", line_start=5, line_end=2)
