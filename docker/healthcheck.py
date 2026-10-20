#!/usr/bin/env python3
"""Container health checks: ``perfpilot-healthcheck api|ui|worker`` (exit 0 = healthy).

* api    — ``GET /api/health`` returns ``status: ok`` (database reachable).
* ui     — Streamlit's ``/_stcore/health`` endpoint answers.
* worker — a job worker process is running and the database is reachable.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=4) as response:  # noqa: S310 - fixed local URL
        body: bytes = response.read()
        return body


def check_api() -> bool:
    return json.loads(_get("http://127.0.0.1:8000/api/health")).get("status") == "ok"


def check_ui() -> bool:
    return _get("http://127.0.0.1:8501/_stcore/health").strip() == b"ok"


def _worker_running() -> bool:
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if b"backend.jobs.worker" in cmdline.read_bytes():
                return True
        except OSError:
            continue
    return False


def check_worker() -> bool:
    if not _worker_running():
        return False
    from sqlalchemy import text

    from backend.core.config import get_settings
    from backend.models.database import build_engine

    engine = build_engine(get_settings())
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()
    return True


CHECKS = {"api": check_api, "ui": check_ui, "worker": check_worker}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in CHECKS:
        print(f"usage: perfpilot-healthcheck {{{'|'.join(CHECKS)}}}", file=sys.stderr)
        return 2
    try:
        return 0 if CHECKS[argv[0]]() else 1
    except Exception as exc:
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
