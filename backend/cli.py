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
    from backend.core.config import get_settings
    from backend.models.database import init_engine

    settings = get_settings()
    init_engine(settings)
    print(f"database ready: {settings.effective_database_url}")
    return 0


def _cmd_drain(_: argparse.Namespace) -> int:
    from backend.core.config import get_settings
    from backend.jobs.worker import Worker
    from backend.models.database import init_engine

    settings = get_settings()
    init_engine(settings)
    print(json.dumps({"jobs_run": Worker(settings).drain()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="perfpilot", description="PerfPilot AI CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worker", help="run the persistent job worker").set_defaults(fn=_cmd_worker)
    sub.add_parser("init-db", help="create database tables").set_defaults(fn=_cmd_init_db)
    sub.add_parser("drain", help="run all queued jobs, then exit").set_defaults(fn=_cmd_drain)
    args = parser.parse_args(argv)
    result: int = args.fn(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
