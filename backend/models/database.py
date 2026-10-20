"""SQLAlchemy engine, session management and declarative base."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from backend.core.config import Settings, get_settings


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    settings.ensure_dirs()
    url = settings.effective_database_url
    kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def _is_memory_database(engine: Engine) -> bool:
    return engine.url.get_backend_name() == "sqlite" and engine.url.database in (
        None,
        "",
        ":memory:",
    )


def init_engine(settings: Settings | None = None, *, prepare_schema: bool = True) -> Engine:
    """(Re)initialise the global engine and bring the schema up to date. Safe to call repeatedly.

    With ``database_schema_mode="migrate"`` (the default) pending Alembic migrations are applied.
    ``create_all`` is used for in-memory databases and when the test suite selects it: each test
    gets a throwaway database, and ``tests/unit/test_migrations.py`` proves the migration head
    produces exactly the same schema. Job child processes pass ``prepare_schema=False`` because
    their parent worker already migrated the database.
    """
    global _engine, _session_factory
    settings = settings or get_settings()
    if _engine is not None:
        _engine.dispose()
    _engine = build_engine(settings)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    import_all_models()
    if prepare_schema:
        if settings.database_schema_mode == "create_all" or _is_memory_database(_engine):
            Base.metadata.create_all(_engine)
        else:
            from backend.models.migrations import upgrade_to_head

            upgrade_to_head(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def import_all_models() -> None:
    """Import every ORM module under ``backend.models.orm`` so metadata is complete."""
    import importlib
    import pkgutil

    import backend.models.orm as orm_pkg

    for info in pkgutil.iter_modules(orm_pkg.__path__):
        importlib.import_module(f"{orm_pkg.__name__}.{info.name}")
