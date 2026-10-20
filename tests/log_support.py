"""Capture PerfPilot's JSON log lines in tests (formatted exactly as in production)."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from backend.core.logging import JsonFormatter


class JsonLines:
    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    def text(self) -> str:
        return self.stream.getvalue()

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text().splitlines() if line]

    def named(self, msg: str) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["msg"] == msg]


@contextmanager
def capture_json_logs(level: int = logging.INFO) -> Iterator[JsonLines]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield JsonLines(stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)
