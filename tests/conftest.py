from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings, get_settings, reset_settings_cache
from backend.models.database import init_engine

# Tests must not depend on a developer's local .env (e.g. a configured LLM, Docker path or
# local-import allowlist); only variables a test sets explicitly apply.
Settings.model_config["env_file"] = None


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Isolated settings: fresh data dir + SQLite DB per test, no LLM configured."""
    monkeypatch.setenv("PERFPILOT_DATA_DIR", str(tmp_path / "data"))
    for var in ("PERFPILOT_LLM_BASE_URL", "PERFPILOT_LLM_API_KEY", "PERFPILOT_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PERFPILOT_JOB_LEASE_SECONDS", "4")
    # Throwaway per-test databases are built from the ORM metadata; tests/unit/test_migrations.py
    # proves the Alembic head yields the identical schema.
    monkeypatch.setenv("PERFPILOT_DATABASE_SCHEMA_MODE", "create_all")
    reset_settings_cache()
    cfg = get_settings()
    init_engine(cfg)
    yield cfg
    reset_settings_cache()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    from backend.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
