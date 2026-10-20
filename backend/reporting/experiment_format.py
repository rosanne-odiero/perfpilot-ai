"""Number formatting shared by the experiment report builder and its renderers."""

from __future__ import annotations

TIMES = "\u00d7"


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 10:
        return f"{value:,.3f} ms"
    return f"{value:,.2f} ms"


def fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.2f}{TIMES}"


def fmt_percent(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}%"


def fmt_bytes(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if abs(value) < 1024 * 1024:
        return f"{value / 1024:,.1f} KiB"
    return f"{value / (1024 * 1024):,.1f} MiB"


def fmt_ci(low: float | None, high: float | None, confidence: float | None = None) -> str:
    if low is None or high is None:
        return "n/a"
    level = f"{round(confidence * 100)}% CI " if confidence else "CI "
    return f"{level}[{low:,.2f}{TIMES}, {high:,.2f}{TIMES}]"
