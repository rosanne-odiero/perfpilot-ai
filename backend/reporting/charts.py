"""Static SVG charts for measured report sections (no network, no external renderer)."""

from __future__ import annotations

from html import escape

from backend.reporting.model import BenchmarkResult

_BASELINE = "#64748b"
_CANDIDATE = "#2563eb"


def benchmark_medians_svg(results: list[BenchmarkResult]) -> str | None:
    """Grouped horizontal bars of baseline vs candidate median wall time per benchmark."""
    if not results:
        return None
    width = 760
    label_w = 220
    bar_h = 14
    group_h = 2 * bar_h + 18
    top = 34
    height = top + group_h * len(results) + 12
    scale_max = max(max(r.baseline_median_ms, r.candidate_median_ms) for r in results) or 1.0
    usable = width - label_w - 120
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Benchmark medians">',
        "<title>Median wall time per benchmark (MEASURED)</title>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        '<g font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#334155">',
        f'<rect x="{label_w}" y="8" width="10" height="10" fill="{_BASELINE}"/>',
        f'<text x="{label_w + 14}" y="17">baseline median</text>',
        f'<rect x="{label_w + 130}" y="8" width="10" height="10" fill="{_CANDIDATE}"/>',
        f'<text x="{label_w + 144}" y="17">candidate median</text>',
    ]
    for i, result in enumerate(results):
        y = top + i * group_h
        name = result.name if len(result.name) <= 34 else result.name[:33] + "…"
        out.append(f'<text x="8" y="{y + bar_h + 4}">{escape(name)}</text>')
        for j, (value, color) in enumerate(
            ((result.baseline_median_ms, _BASELINE), (result.candidate_median_ms, _CANDIDATE))
        ):
            bar_y = y + j * (bar_h + 2)
            bar_w = max(1.0, value / scale_max * usable)
            out.append(
                f'<rect x="{label_w}" y="{bar_y}" width="{bar_w:.1f}" height="{bar_h}" '
                f'fill="{color}"/>'
            )
            out.append(
                f'<text x="{label_w + bar_w + 6:.1f}" y="{bar_y + bar_h - 3}">{value:.2f} ms</text>'
            )
    out.append("</g></svg>")
    return "".join(out)
