"""Structured JSON logging with secret scrubbing."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|token|secret|password|authorization)(\"?\s*[:=]\s*\"?)([^\s\"',]+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"https://[^/\s:@]+:[^/\s@]+@"),
]

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message"}


def scrub(text: str) -> str:
    """Remove obvious credentials from a log string."""
    text = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
    for pattern in _SECRET_PATTERNS[1:]:
        text = pattern.sub("[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = scrub(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "urllib3", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
