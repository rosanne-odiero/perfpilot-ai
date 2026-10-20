"""Inline SVG box/strip plot of raw benchmark samples (no network, no external renderer)."""

from __future__ import annotations

import math
import statistics
from html import escape

from backend.reporting.experiment_model import SampleRow

_COLORS = {"baseline": "#64748b", "candidate": "#2563eb"}
_WIDTH = 760
_LEFT = 120
_RIGHT = 30
_ROW_H = 74
_TOP = 22
_AXIS_H = 34
LOG_SCALE_RATIO = 20.0


def timing_values(samples: list[SampleRow]) -> dict[str, list[float]]:
    values: dict[str, list[float]] = {"baseline": [], "candidate": []}
    for sample in samples:
        if sample.kind == "timing" and sample.ok and sample.wall_ms is not None:
            values.setdefault(sample.variant, []).append(sample.wall_ms)
    return {k: v for k, v in values.items() if v}


def uses_log_scale(values: dict[str, list[float]]) -> bool:
    flat = [v for vs in values.values() for v in vs]
    return bool(flat) and min(flat) > 0 and max(flat) / min(flat) > LOG_SCALE_RATIO


def _ticks(lo: float, hi: float, log: bool) -> list[float]:
    if log:
        first, last = math.floor(math.log10(lo)), math.ceil(math.log10(hi))
        return [10.0**e for e in range(first, last + 1) if lo <= 10.0**e <= hi]
    span = hi - lo
    raw = span / 5 if span > 0 else 1.0
    magnitude = 10 ** math.floor(math.log10(raw))
    step = min((m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw), default=raw)
    start = math.ceil(lo / step) * step
    ticks: list[float] = []
    tick = start
    while tick <= hi + step * 1e-9 and len(ticks) < 12:
        ticks.append(tick)
        tick += step
    return ticks


def _tick_label(value: float) -> str:
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:g}"
    return f"{value:.3g}"


def samples_box_strip_svg(samples: list[SampleRow]) -> str | None:
    """Box (IQR), median line, min-max whiskers and every timed run as a dot, per variant."""
    values = timing_values(samples)
    if not values:
        return None
    log = uses_log_scale(values)
    flat = [v for vs in values.values() for v in vs]
    lo, hi = min(flat), max(flat)
    if log:
        lo, hi = lo / 1.25, hi * 1.25
    else:
        pad = (hi - lo) * 0.08 or max(hi * 0.05, 1e-6)
        lo, hi = max(0.0, lo - pad), hi + pad
    usable = _WIDTH - _LEFT - _RIGHT

    def x(value: float) -> float:
        if log:
            pos = (math.log10(value) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
        else:
            pos = (value - lo) / (hi - lo) if hi > lo else 0.5
        return _LEFT + pos * usable

    rows = [v for v in ("baseline", "candidate") if v in values]
    height = _TOP + _ROW_H * len(rows) + _AXIS_H
    axis_y = _TOP + _ROW_H * len(rows)
    scale = "log scale" if log else "linear scale"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{height}" '
        f'viewBox="0 0 {_WIDTH} {height}" role="img" aria-label="Benchmark samples">',
        f"<title>Raw wall-time samples per variant (MEASURED, ms, {scale})</title>",
        f'<rect x="0" y="0" width="{_WIDTH}" height="{height}" fill="#ffffff"/>',
        '<g font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#334155">',
    ]
    for tick in _ticks(lo, hi, log):
        tx = x(tick)
        out.append(
            f'<line x1="{tx:.1f}" y1="{_TOP - 6}" x2="{tx:.1f}" y2="{axis_y}" '
            'stroke="#e2e8f0" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{tx:.1f}" y="{axis_y + 14}" text-anchor="middle">'
            f"{escape(_tick_label(tick))}</text>"
        )
    out.append(
        f'<line x1="{_LEFT}" y1="{axis_y}" x2="{_WIDTH - _RIGHT}" y2="{axis_y}" stroke="#94a3b8"/>'
    )
    out.append(
        f'<text x="{_LEFT + usable / 2:.1f}" y="{axis_y + 29}" text-anchor="middle">'
        f"wall time per call (ms, {scale})</text>"
    )
    for row, variant in enumerate(rows):
        data = sorted(values[variant])
        color = _COLORS.get(variant, "#0f766e")
        cy = _TOP + row * _ROW_H + _ROW_H / 2
        median = statistics.median(data)
        q1, q3 = (
            statistics.quantiles(data, n=4, method="inclusive")[::2]
            if len(data) >= 2
            else (data[0], data[0])
        )
        out.append(f'<text x="8" y="{cy - 4:.1f}" font-weight="bold">{escape(variant)}</text>')
        out.append(f'<text x="8" y="{cy + 10:.1f}">n={len(data)}</text>')
        out.append(
            f'<line x1="{x(data[0]):.1f}" y1="{cy:.1f}" x2="{x(data[-1]):.1f}" y2="{cy:.1f}" '
            f'stroke="{color}" stroke-width="1.5"/>'
        )
        for edge in (data[0], data[-1]):
            out.append(
                f'<line x1="{x(edge):.1f}" y1="{cy - 8:.1f}" x2="{x(edge):.1f}" '
                f'y2="{cy + 8:.1f}" stroke="{color}" stroke-width="1.5"/>'
            )
        box_x = x(q1)
        box_w = max(1.5, x(q3) - box_x)
        out.append(
            f'<rect x="{box_x:.1f}" y="{cy - 14:.1f}" width="{box_w:.1f}" height="28" '
            f'fill="{color}" fill-opacity="0.18" stroke="{color}" stroke-width="1.5"/>'
        )
        out.append(
            f'<line x1="{x(median):.1f}" y1="{cy - 14:.1f}" x2="{x(median):.1f}" '
            f'y2="{cy + 14:.1f}" stroke="{color}" stroke-width="3"/>'
        )
        for i, value in enumerate(values[variant]):
            jitter = ((i * 37) % 11 - 5) * 2.2
            out.append(
                f'<circle cx="{x(value):.1f}" cy="{cy + jitter:.1f}" r="2.6" fill="{color}" '
                'fill-opacity="0.75"/>'
            )
        out.append(
            f'<text x="{x(median):.1f}" y="{cy - 18:.1f}" text-anchor="middle">'
            f"median {escape(_tick_label(median))} ms</text>"
        )
    out.append("</g></svg>")
    return "".join(out)
