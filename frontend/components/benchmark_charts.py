"""Benchmark charts from raw samples: distributions, paired ratios and memory (pure builders)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from frontend.formatting import fmt_ratio
from frontend.theme import VARIANT_COLORS, plotly_template

CLASSIFICATION_COLORS = {
    "IMPROVED": "#16a34a",
    "REGRESSED": "#dc2626",
    "NO_CLEAR_DIFFERENCE": "#64748b",
    "INCONCLUSIVE": "#ca8a04",
    "INVALID": "#dc2626",
}
VARIANTS = ("baseline", "candidate")
NOISY_CV_PERCENT = 10.0


def timing_frame(samples: list[dict[str, Any]]) -> pd.DataFrame:
    """One row per successful timing sample, in milliseconds."""
    rows = [
        {
            "variant": s["variant"],
            "pair_index": s["pair_index"],
            "position_in_pair": s["position_in_pair"],
            "wall_ms": s["wall_ns"] / 1e6,
            "cpu_ms": s["cpu_ns"] / 1e6 if s.get("cpu_ns") is not None else None,
        }
        for s in samples
        if s["kind"] == "timing" and s.get("ok") and s.get("wall_ns") is not None
    ]
    return pd.DataFrame(
        rows, columns=["variant", "pair_index", "position_in_pair", "wall_ms", "cpu_ms"]
    )


def memory_frame(samples: list[dict[str, Any]]) -> pd.DataFrame:
    rows = [
        {
            "variant": s["variant"],
            "tracemalloc_peak_bytes": s.get("tracemalloc_peak_bytes"),
            "rss_peak_bytes": s.get("rss_peak_bytes"),
        }
        for s in samples
        if s["kind"] == "memory" and s.get("ok")
    ]
    return pd.DataFrame(rows, columns=["variant", "tracemalloc_peak_bytes", "rss_peak_bytes"])


def paired_ratios(frame: pd.DataFrame, metric: str = "wall_ms") -> pd.DataFrame:
    """Per-pair baseline/candidate ratio (>1 means the candidate was faster in that pair)."""
    if frame.empty:
        return pd.DataFrame(columns=["pair_index", "ratio"])
    wide = frame.pivot_table(index="pair_index", columns="variant", values=metric, aggfunc="first")
    wide = wide.dropna(subset=[v for v in VARIANTS if v in wide.columns])
    if not set(VARIANTS) <= set(wide.columns):
        return pd.DataFrame(columns=["pair_index", "ratio"])
    ratios = (wide["baseline"] / wide["candidate"]).rename("ratio").reset_index()
    return ratios.sort_values("pair_index").reset_index(drop=True)


def distribution_figure(
    frame: pd.DataFrame, *, metric: str = "wall_ms", log_y: bool = False, labels: dict[str, str]
) -> go.Figure:
    figure = go.Figure()
    for variant in VARIANTS:
        values = frame.loc[frame["variant"] == variant, metric].dropna()
        if values.empty:
            continue
        figure.add_trace(
            go.Box(
                y=values,
                name=labels.get(variant, variant),
                boxpoints="all",
                jitter=0.45,
                pointpos=0,
                marker={"color": VARIANT_COLORS[variant], "size": 6, "opacity": 0.75},
                line={"color": VARIANT_COLORS[variant]},
                fillcolor="rgba(0,0,0,0)",
                boxmean=False,
                hovertemplate="%{y:.4g} ms<extra>%{fullData.name}</extra>",
            )
        )
    figure.update_layout(
        template=plotly_template(),
        height=380,
        showlegend=False,
        yaxis={
            "title": "wall time per call (ms)" if metric == "wall_ms" else "CPU time (ms)",
            "type": "log" if log_y else "linear",
        },
        title={"text": "Raw samples per run (box = median and quartiles)", "font": {"size": 13}},
    )
    return figure


def ratio_figure(
    ratios: pd.DataFrame, *, speedup: float | None, ci: tuple[float | None, float | None]
) -> go.Figure:
    figure = go.Figure()
    low, high = ci
    if low is not None and high is not None:
        figure.add_hrect(
            y0=low,
            y1=high,
            fillcolor="rgba(139,92,246,0.18)",
            line_width=0,
            annotation_text="95% CI",
            annotation_position="top left",
        )
    figure.add_hline(
        y=1.0,
        line={"color": "rgba(148,163,184,0.6)", "dash": "dot"},
        annotation_text="no change",
        annotation_position="bottom right",
    )
    if speedup is not None:
        figure.add_hline(
            y=speedup,
            line={"color": "#8b5cf6"},
            annotation_text=f"speedup {fmt_ratio(speedup)}",
            annotation_position="top right",
        )
    figure.add_trace(
        go.Scatter(
            x=ratios["pair_index"],
            y=ratios["ratio"],
            mode="markers",
            marker={"color": "#22d3ee", "size": 8},
            hovertemplate="pair %{x}: %{y:.3g}x<extra></extra>",
        )
    )
    figure.update_layout(
        template=plotly_template(),
        height=380,
        showlegend=False,
        xaxis={"title": "pair index (run order)"},
        yaxis={"title": "baseline / candidate"},
        title={"text": "Per-pair ratio (paired A/B)", "font": {"size": 13}},
    )
    return figure


def memory_figure(frame: pd.DataFrame, labels: dict[str, str]) -> go.Figure:
    """tracemalloc and RSS side by side: different things, so never on one axis."""
    figure = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=(
            "tracemalloc peak (Python allocations in the call)",
            "RSS peak (whole child process)",
        ),
    )
    for col, metric in enumerate(("tracemalloc_peak_bytes", "rss_peak_bytes"), start=1):
        for variant in VARIANTS:
            values = frame.loc[frame["variant"] == variant, metric].dropna()
            if values.empty:
                continue
            figure.add_trace(
                go.Box(
                    y=values / 1024,
                    name=labels.get(variant, variant),
                    boxpoints="all",
                    pointpos=0,
                    jitter=0.3,
                    marker={"color": VARIANT_COLORS[variant], "size": 7},
                    line={"color": VARIANT_COLORS[variant]},
                    fillcolor="rgba(0,0,0,0)",
                    showlegend=False,
                    hovertemplate="%{y:,.1f} KiB<extra>%{fullData.name}</extra>",
                ),
                row=1,
                col=col,
            )
        figure.update_yaxes(title_text="KiB", row=1, col=col)
    figure.update_layout(template=plotly_template(), height=360)
    figure.update_annotations(font_size=12)
    return figure


def noise_warnings(run: dict[str, Any]) -> list[str]:
    """Warnings reported by the API, plus high run-to-run variation it did not already flag."""
    warnings = [str(w) for w in run.get("warnings") or []]
    summary = run.get("summary") or {}
    for variant in VARIANTS:
        stats = (summary.get(variant) or {}).get("wall_ms") or {}
        cv = stats.get("cv_percent")
        already_reported = any(w.startswith(f"{variant} wall time") for w in warnings)
        if cv is not None and cv > NOISY_CV_PERCENT and not already_reported:
            warnings.append(
                f"{variant} wall time varies a lot between runs (CV {cv:.1f}% > "
                f"{NOISY_CV_PERCENT:.0f}%); small differences may be noise."
            )
    return list(dict.fromkeys(warnings))
