"""Helpers shared by all report renderers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.models.enums import ReportFormat
from backend.reporting.model import AuditReport

_SLUG = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RenderedFile:
    role: str
    filename: str
    media_type: str
    data: bytes


@dataclass
class RenderResult:
    format: ReportFormat
    files: list[RenderedFile]
    redaction_count: int
    redaction_kinds: dict[str, int] = field(default_factory=dict)
    engine: str | None = None

    @property
    def primary(self) -> RenderedFile:
        return self.files[0]


def slug(value: str, limit: int = 60) -> str:
    cleaned = _SLUG.sub("-", value.lower()).strip("-")
    return cleaned[:limit].strip("-") or "repository"


def report_filename(report: AuditReport, extension: str, suffix: str = "audit") -> str:
    """``perfpilot-<repo>-<sha7>-<suffix>.<ext>``"""
    sha = report.repository.short_sha or "nosha"
    return f"perfpilot-{slug(report.repository.name)}-{slug(sha, 12)}-{suffix}.{extension}"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def humanize(value: str) -> str:
    return value.replace("_", " ").lower()
