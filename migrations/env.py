"""Alembic environment for PerfPilot.

Programmatic callers (``backend.models.migrations``) pass an open connection through
``config.attributes["connection"]``. The ``alembic`` CLI (``alembic.ini``) falls back to the
database configured by ``PERFPILOT_*`` settings.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection

from backend.core.config import get_settings
from backend.models.database import Base, build_engine, import_all_models

import_all_models()
target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode rebuilds tables when needed.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_offline() -> None:
    context.configure(
        url=get_settings().effective_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = build_engine(get_settings())
    try:
        with engine.begin() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
