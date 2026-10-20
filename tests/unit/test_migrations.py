"""Alembic migrations: the head revision must reproduce the ORM metadata exactly.

If ``test_migration_head_matches_orm_metadata`` fails, a model changed without a migration: run
``make migration m="describe the change"`` (or ``make migration-baseline`` while ``0001`` is still
the only revision) and commit the generated file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect, text

from backend.cli import main as cli_main
from backend.core.config import Settings, reset_settings_cache
from backend.models import migrations
from backend.models.database import Base, build_engine, import_all_models, init_engine


def _file_engine(tmp_path: Path) -> Engine:
    return create_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")


def test_migration_head_matches_orm_metadata() -> None:
    assert migrations.check() == []


def test_single_linear_history_with_baseline_root() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrations.alembic_config())
    assert len(script.get_heads()) == 1
    assert script.get_base() == migrations.BASELINE_REVISION


def test_upgrade_creates_every_table_and_is_idempotent(tmp_path: Path) -> None:
    import_all_models()
    engine = _file_engine(tmp_path)
    head = migrations.head_revision()
    assert migrations.upgrade_to_head(engine) == head
    assert migrations.upgrade_to_head(engine) == head
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert migrations.VERSION_TABLE in tables
    with engine.connect() as conn:
        assert migrations.schema_diff(conn) == []
    engine.dispose()


def test_downgrade_to_base_and_back(tmp_path: Path) -> None:
    from alembic import command

    engine = _file_engine(tmp_path)
    with engine.begin() as conn:
        cfg = migrations.alembic_config(conn)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        assert set(inspect(conn).get_table_names()) <= {migrations.VERSION_TABLE}
        command.upgrade(cfg, "head")
        assert migrations.current_revision(conn) == migrations.head_revision()
    engine.dispose()


def _legacy_database(tmp_path: Path) -> Engine:
    """A database shaped like the baseline but created without Alembic bookkeeping."""
    from alembic import command

    engine = _file_engine(tmp_path)
    with engine.begin() as conn:
        command.upgrade(migrations.alembic_config(conn), migrations.BASELINE_REVISION)
        conn.execute(text(f"DROP TABLE {migrations.VERSION_TABLE}"))
    return engine


def test_pre_migration_database_matching_baseline_is_stamped(tmp_path: Path) -> None:
    engine = _legacy_database(tmp_path)
    assert migrations.upgrade_to_head(engine) == migrations.head_revision()
    engine.dispose()


def test_pre_migration_database_with_unknown_shape_is_refused(tmp_path: Path) -> None:
    engine = _legacy_database(tmp_path)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN surprise INTEGER"))
    with pytest.raises(migrations.SchemaMigrationError, match="does not match the baseline"):
        migrations.upgrade_to_head(engine)
    engine.dispose()


def test_init_engine_migrates_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PERFPILOT_DATABASE_SCHEMA_MODE", raising=False)
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.database_schema_mode == "migrate"
    engine = init_engine(settings)
    with engine.connect() as conn:
        assert migrations.current_revision(conn) == migrations.head_revision()


def test_create_all_mode_skips_alembic(tmp_path: Path) -> None:
    engine = init_engine(Settings(data_dir=tmp_path / "data", database_schema_mode="create_all"))
    assert migrations.VERSION_TABLE not in inspect(engine).get_table_names()


def test_cli_init_db_runs_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PERFPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PERFPILOT_DATABASE_SCHEMA_MODE", raising=False)
    reset_settings_cache()
    try:
        assert cli_main(["init-db"]) == 0
    finally:
        reset_settings_cache()
    assert f"revision {migrations.head_revision()}" in capsys.readouterr().out
    engine = build_engine(Settings(data_dir=tmp_path / "data"))
    with engine.connect() as conn:
        assert migrations.current_revision(conn) == migrations.head_revision()
    engine.dispose()


@pytest.fixture
def scratch_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "migrations"
    shutil.copytree(migrations.MIGRATIONS_DIR, target, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(migrations, "MIGRATIONS_DIR", target)
    return target


def test_new_revision_is_noop_without_model_changes(scratch_migrations: Path) -> None:
    assert migrations.new_revision("nothing changed") is None
    assert len(list((scratch_migrations / "versions").glob("*.py"))) == 1


def test_baseline_cannot_be_rewritten_once_later_revisions_exist(
    scratch_migrations: Path,
) -> None:
    (scratch_migrations / "versions" / "0002_later.py").write_text(
        'revision = "0002"\ndown_revision = "0001"\n'
        "def upgrade():\n    pass\n\ndef downgrade():\n    pass\n"
    )
    with pytest.raises(migrations.SchemaMigrationError, match="later revisions exist"):
        migrations.regenerate_baseline()


def test_migrations_cli_check_reports_in_sync(capsys: pytest.CaptureFixture[str]) -> None:
    assert migrations.main(["check"]) == 0
    assert '"in_sync": true' in capsys.readouterr().out
