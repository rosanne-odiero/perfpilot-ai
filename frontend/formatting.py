"""Pure formatting helpers shared by pages and components (no Streamlit imports)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}
PROVENANCES = ("MEASURED", "STATIC", "AI_HYPOTHESIS")
TIMES = "\u00d7"
TERMINAL_JOB_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"})

SOURCE_LABELS = {
    "GITHUB_PUBLIC": "GitHub",
    "GITHUB_PRIVATE": "GitHub (private)",
    "ZIP_UPLOAD": "ZIP upload",
    "LOCAL_DIRECTORY": "Local directory",
}

_GITHUB_REPO = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:/tree/[^\s?#]+)?/?$"
)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,64}$")


def short_sha(sha: str | None, length: int = 8) -> str:
    return sha[:length] if sha else "—"


def human_bytes(value: float | None) -> str:
    if value is None:
        return "—"
    size = float(value)
    if abs(size) < 1024:
        return f"{size:.0f} B"
    units = ("KB", "MB", "GB")
    index = 0
    size /= 1024
    while abs(size) >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def fmt_ms(value: float | None) -> str:
    """Milliseconds with a unit that keeps 3 significant digits readable."""
    if value is None:
        return "—"
    if value < 1:
        return f"{value * 1000:.1f} µs"
    if value < 1000:
        return f"{value:.3g} ms" if value < 100 else f"{value:.0f} ms"
    return f"{value / 1000:.2f} s"


def fmt_ratio(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}{TIMES}" if value < 10 else f"{value:.1f}{TIMES}"


def fmt_pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}%"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def fmt_dt(value: str | None) -> str:
    parsed = parse_dt(value)
    return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else "—"


def fmt_duration(start: str | None, end: str | None) -> str:
    begin, finish = parse_dt(start), parse_dt(end)
    if begin is None or finish is None:
        return "—"
    seconds = (finish - begin).total_seconds()
    return f"{seconds:.1f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


def github_permalink(
    source_type: str | None,
    locator: str | None,
    commit_sha: str | None,
    file_path: str,
    line_start: int,
    line_end: int | None = None,
) -> str | None:
    """Link to the exact lines at the exact commit; ``None`` unless the source is GitHub."""
    if source_type not in {"GITHUB_PUBLIC", "GITHUB_PRIVATE"} or not locator:
        return None
    match = _GITHUB_REPO.match(locator.strip())
    if match is None or commit_sha is None or not _COMMIT_SHA.match(commit_sha):
        return None
    if file_path.startswith("/") or ".." in file_path.split("/"):
        return None
    anchor = f"L{line_start}"
    if line_end is not None and line_end > line_start:
        anchor += f"-L{line_end}"
    base = f"https://github.com/{match['owner']}/{match['repo']}"
    return f"{base}/blob/{commit_sha}/{quote(file_path)}#{anchor}"


def max_severity(by_severity: dict[str, int]) -> str | None:
    present = [s for s, n in by_severity.items() if n and s in SEVERITY_RANK]
    return min(present, key=SEVERITY_RANK.__getitem__) if present else None


def humanize(identifier: str) -> str:
    return identifier.replace("_", " ").capitalize()


def items_of(payload: Any) -> list[dict[str, Any]]:
    """Objects of a list payload or of a page object's ``items``; anything else is empty."""
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        payload = payload["items"]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []
