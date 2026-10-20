"""``perfpilot`` command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys


def _cmd_worker(_: argparse.Namespace) -> int:
    from backend.jobs.worker import main as worker_main

    worker_main()
    return 0


def _cmd_init_db(_: argparse.Namespace) -> int:
    """Apply Alembic migrations (``alembic upgrade head``) to the configured database."""
    from backend.core.config import get_settings
    from backend.models.database import build_engine
    from backend.models.migrations import upgrade_to_head

    settings = get_settings()
    engine = build_engine(settings)
    try:
        revision = upgrade_to_head(engine)
    finally:
        engine.dispose()
    print(f"database ready at revision {revision}: {engine.url.render_as_string()}")
    return 0


def _cmd_drain(_: argparse.Namespace) -> int:
    from backend.core.config import get_settings
    from backend.jobs.worker import Worker
    from backend.models.database import init_engine

    settings = get_settings()
    init_engine(settings)
    print(json.dumps({"jobs_run": Worker(settings).drain()}))
    return 0


def _cmd_prune_workspaces(args: argparse.Namespace) -> int:
    """Delete imported-repository working copies (read-only trees) to reclaim disk space.

    Snapshot records, audits and reports are kept; a pruned snapshot must be re-imported before
    it can be audited again.
    """
    from datetime import timedelta

    from sqlalchemy import select

    from backend.core.config import get_settings
    from backend.ingestion.workspace import cleanup_workspace, remove_tree
    from backend.models.database import init_engine, session_scope, utcnow
    from backend.models.orm.audit import RepositorySnapshot

    settings = get_settings()
    init_engine(settings)
    cutoff = utcnow() - timedelta(days=args.older_than_days)
    removed: list[str] = []
    with session_scope() as session:
        snapshots = {s.id: s for s in session.scalars(select(RepositorySnapshot))}
        for snapshot in snapshots.values():
            created = snapshot.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=cutoff.tzinfo)
            if (args.all or created < cutoff) and cleanup_workspace(settings, snapshot.id):
                snapshot.workspace_path = None
                removed.append(snapshot.id)
    if settings.workspaces_dir.is_dir():
        for child in settings.workspaces_dir.iterdir():
            if child.is_dir() and not child.is_symlink() and child.name not in snapshots:
                remove_tree(child)
                removed.append(child.name)
    print(json.dumps({"removed_workspaces": removed}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perfpilot", description="PerfPilot AI CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worker", help="run the persistent job worker").set_defaults(fn=_cmd_worker)
    sub.add_parser("init-db", help="apply database migrations").set_defaults(fn=_cmd_init_db)
    sub.add_parser("drain", help="run all queued jobs, then exit").set_defaults(fn=_cmd_drain)
    prune = sub.add_parser(
        "prune-workspaces", help="delete imported repository working copies (keeps results)"
    )
    prune.add_argument("--older-than-days", type=int, default=7)
    prune.add_argument("--all", action="store_true", help="prune every snapshot workspace")
    prune.set_defaults(fn=_cmd_prune_workspaces)
    args = parser.parse_args(argv)
    result: int = args.fn(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
