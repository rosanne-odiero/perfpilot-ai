"""Alembic integration: apply, verify and generate schema migrations.

The migration scripts live in ``migrations/`` at the repository root. Every function here runs
Alembic against an explicit SQLAlchemy connection (``config.attributes["connection"]``), so the
database URL never round-trips through ``alembic.ini``.

Command line (used by the Makefile)::

    python -m backend.models.migrations check            # exit 1 if the ORM drifted from head
    python -m backend.models.migrations revision -m msg  # autogenerate the next revision
    python -m backend.models.migrations baseline         # regenerate the single baseline revision
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, inspect
from sqlalchemy.pool import StaticPool

from backend.core.exceptions import PerfPilotError
from backend.core.logging import get_logger

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
VERSION_TABLE = "alembic_version"
BASELINE_REVISION = "0001"


class SchemaMigrationError(PerfPilotError):
    code = "schema_migration_error"
    status_code = 500


def alembic_config(connection: Connection | None = None) -> Config:
    """Programmatic Alembic config bound to ``connection`` (if given)."""
    if not (MIGRATIONS_DIR / "env.py").is_file():
        raise SchemaMigrationError(f"migration scripts not found at {MIGRATIONS_DIR}")
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("path_separator", "os")
    # Generated revisions are normalised with ruff (a dev dependency) when it is installed.
    cfg.set_section_option("post_write_hooks", "hooks", "ruff_format, ruff_fix")
    cfg.set_section_option("post_write_hooks", "ruff_fix.type", "module")
    cfg.set_section_option("post_write_hooks", "ruff_fix.module", "ruff")
    cfg.set_section_option(
        "post_write_hooks", "ruff_fix.options", "check --fix --silent REVISION_SCRIPT_FILENAME"
    )
    cfg.set_section_option("post_write_hooks", "ruff_format.type", "module")
    cfg.set_section_option("post_write_hooks", "ruff_format.module", "ruff")
    cfg.set_section_option(
        "post_write_hooks", "ruff_format.options", "format --quiet REVISION_SCRIPT_FILENAME"
    )
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def schema_diff(connection: Connection) -> list[Any]:
    """Differences between the live schema and the ORM metadata (empty = in sync)."""
    from backend.models.database import Base, import_all_models

    import_all_models()
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "render_as_batch": True}
    )
    return list(compare_metadata(context, Base.metadata))


@contextlib.contextmanager
def _migration_lock(engine: Engine) -> Iterator[None]:
    """Serialise concurrent upgrades (API and worker start together) on a file database."""
    database = engine.url.database if engine.url.get_backend_name() == "sqlite" else None
    if not database or database == ":memory:":
        yield
        return
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX platforms rely on SQLite's own locking
        yield
        return
    lock_path = Path(database).with_name(Path(database).name + ".migrate.lock")
    with lock_path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def upgrade_to_head(engine: Engine) -> str | None:
    """Apply pending migrations and return the resulting revision.

    A database created before migrations existed (by ``create_all``) has application tables but
    no ``alembic_version``. It is stamped at the baseline only when its schema matches the
    baseline exactly; anything else is refused rather than guessed at.
    """
    with _migration_lock(engine), engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        before = current_revision(connection)
        cfg = alembic_config(connection)
        if before is None and tables - {VERSION_TABLE}:
            diff = _baseline_diff(connection)
            if diff:
                raise SchemaMigrationError(
                    "database was created without migrations and does not match the baseline "
                    "schema; back it up and recreate it, or stamp it manually with alembic",
                    details={"differences": len(diff)},
                )
            command.stamp(cfg, BASELINE_REVISION)
            log.info("stamped pre-migration database", extra={"revision": BASELINE_REVISION})
        command.upgrade(cfg, "head")
        after = current_revision(connection)
    if after != before:
        log.info("database migrated", extra={"from_revision": before, "to_revision": after})
    return after


def _baseline_diff(connection: Connection) -> list[Any]:
    """Compare a live schema with the schema produced by the baseline revision alone."""
    reference = create_engine("sqlite://", poolclass=StaticPool)
    with reference.begin() as ref_conn:
        command.upgrade(alembic_config(ref_conn), BASELINE_REVISION)
        live = _schema_signature(connection)
        expected = _schema_signature(ref_conn)
    reference.dispose()
    return [
        name for name in sorted(set(live) | set(expected)) if live.get(name) != expected.get(name)
    ]


def _schema_signature(connection: Connection) -> dict[str, list[tuple[str, str, bool]]]:
    inspector = inspect(connection)
    return {
        table: sorted(
            (col["name"], str(col["type"]), bool(col["nullable"]))
            for col in inspector.get_columns(table)
        )
        for table in inspector.get_table_names()
        if table != VERSION_TABLE
    }


# --------------------------------------------------------------------------- authoring helpers
@contextlib.contextmanager
def _scratch_connection() -> Iterator[Connection]:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def check() -> list[str]:
    """Upgrade an empty database to head and report drift from the ORM metadata."""
    with _scratch_connection() as connection:
        command.upgrade(alembic_config(connection), "head")
        return [repr(item) for item in schema_diff(connection)]


def _next_revision_id() -> str:
    script = ScriptDirectory.from_config(alembic_config())
    numbers = [int(rev.revision) for rev in script.walk_revisions() if rev.revision.isdigit()]
    return f"{max(numbers, default=0) + 1:04d}"


def new_revision(message: str) -> Path | None:
    """Autogenerate the next revision from the diff between head and the ORM metadata."""
    with _scratch_connection() as connection:
        cfg = alembic_config(connection)
        command.upgrade(cfg, "head")
        if not schema_diff(connection):
            return None
        script = command.revision(
            cfg, message=message, autogenerate=True, rev_id=_next_revision_id()
        )
    return _script_path(script)


def regenerate_baseline() -> Path:
    """Replace the baseline with one generated from the current ORM metadata.

    Only valid while ``0001`` is the sole revision (i.e. before any release shipped a database).
    """
    versions = MIGRATIONS_DIR / "versions"
    existing = sorted(versions.glob("*.py"))
    others = [p for p in existing if not p.name.startswith(f"{BASELINE_REVISION}_")]
    if others:
        raise SchemaMigrationError(
            "later revisions exist; add a new revision instead of rewriting the baseline",
            details={"revisions": [p.name for p in others]},
        )
    for old in existing:
        old.unlink()
    with _scratch_connection() as connection:
        script = command.revision(
            alembic_config(connection),
            message="baseline schema",
            autogenerate=True,
            rev_id=BASELINE_REVISION,
        )
    written = _script_path(script)
    if written is None:  # pragma: no cover - alembic always returns a script for one head
        raise SchemaMigrationError("alembic did not write a baseline revision")
    return written


def _script_path(script: object) -> Path | None:
    path = getattr(script, "path", None)
    return Path(path) if path else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.models.migrations")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="fail if the ORM metadata differs from the migration head")
    rev = sub.add_parser("revision", help="autogenerate the next migration")
    rev.add_argument("-m", "--message", required=True)
    sub.add_parser("baseline", help="regenerate the baseline (only while it is the sole revision)")
    args = parser.parse_args(argv)
    if args.command == "check":
        diff = check()
        print(json.dumps({"head": head_revision(), "in_sync": not diff, "differences": diff}))
        return 1 if diff else 0
    if args.command == "revision":
        created = new_revision(args.message)
        print(f"created {created}" if created else "no schema changes detected")
        return 0
    print(f"baseline written to {regenerate_baseline()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
