"""Shared visual styling: CSS, badges and the Plotly template used by every chart."""

from __future__ import annotations

import html

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

PROVENANCE_COLORS = {
    "MEASURED": "#16a34a",
    "STATIC": "#2563eb",
    "AI_HYPOTHESIS": "#a855f7",
}

SEVERITY_COLORS = {
    "CRITICAL": "#dc2626",
    "HIGH": "#ea580c",
    "MEDIUM": "#ca8a04",
    "LOW": "#0891b2",
    "INFO": "#64748b",
}

STATUS_COLORS = {
    "SUCCEEDED": "#16a34a",
    "COMPLETED": "#16a34a",
    "READY": "#16a34a",
    "PARTIAL": "#ca8a04",
    "RUNNING": "#6366f1",
    "QUEUED": "#64748b",
    "PENDING": "#64748b",
    "FAILED": "#dc2626",
    "CANCELLED": "#64748b",
    "TIMED_OUT": "#dc2626",
}

ACCENT = "#8b5cf6"
ACCENT_2 = "#22d3ee"
VARIANT_COLORS = {"baseline": "#f43f5e", "candidate": "#22c55e"}
PLOTLY_TEMPLATE = "perfpilot"

_CSS = """
<style>
  .block-container {padding-top: 1.4rem; max-width: 1480px;}
  [data-testid="stSidebar"] {border-right: 1px solid rgba(148,163,184,0.15);}
  .pp-brand {display:flex; align-items:center; gap:10px; margin:2px 0 6px 0;}
  .pp-brand-logo {width:38px; height:38px; border-radius:10px; display:flex; align-items:center;
    justify-content:center; font-size:20px; color:white;
    background:linear-gradient(135deg,#7c3aed 0%,#2563eb 60%,#06b6d4 100%);
    box-shadow:0 4px 18px rgba(124,58,237,0.35);}
  .pp-brand-name {font-weight:700; font-size:1.08rem; line-height:1.1;}
  .pp-brand-name span {background:linear-gradient(90deg,#a78bfa,#22d3ee);
    -webkit-background-clip:text; background-clip:text; color:transparent;}
  .pp-brand-tag {font-size:0.74rem; color:#94a3b8;}
  .pp-hero {border:1px solid rgba(139,92,246,0.35); border-radius:16px; padding:22px 26px;
    margin-bottom:18px;
    background:radial-gradient(1200px 300px at 0% 0%, rgba(124,58,237,0.22), transparent 60%),
               radial-gradient(800px 260px at 100% 100%, rgba(6,182,212,0.14), transparent 60%),
               rgba(15,23,42,0.55);}
  .pp-hero h1 {font-size:1.9rem; margin:0 0 6px 0; padding:0; line-height:1.2;}
  .pp-hero h1 em {font-style:normal; background:linear-gradient(90deg,#a78bfa,#38bdf8);
    -webkit-background-clip:text; background-clip:text; color:transparent;}
  .pp-hero p {color:#cbd5e1; margin:0 0 12px 0; font-size:1rem;}
  .pp-chip {display:inline-flex; align-items:center; gap:6px; margin:0 8px 6px 0;
    padding:4px 10px; border-radius:999px; font-size:0.78rem; color:#e2e8f0;
    background:rgba(148,163,184,0.12); border:1px solid rgba(148,163,184,0.22);}
  .pp-card {border:1px solid rgba(148,163,184,0.2); border-radius:14px; padding:14px 16px;
    background:rgba(148,163,184,0.06); margin-bottom:10px;}
  .pp-card h4 {margin:0 0 8px 0; font-size:0.95rem;}
  .pp-metric-grid {display:grid; gap:12px; margin:4px 0 14px 0;
    grid-template-columns:repeat(auto-fit, minmax(170px, 1fr));}
  .pp-metric {border:1px solid rgba(148,163,184,0.2); border-radius:14px; padding:12px 14px;
    background:linear-gradient(180deg, rgba(148,163,184,0.09), rgba(148,163,184,0.03));}
  .pp-metric-label {font-size:0.76rem; color:#94a3b8; text-transform:uppercase;
    letter-spacing:0.04em; display:flex; justify-content:space-between; gap:6px;}
  .pp-metric-label .pp-badge {flex-shrink:0; align-self:flex-start;}
  .pp-metric-value {font-size:1.45rem; font-weight:700; margin-top:4px; line-height:1.2;
    word-break:break-word;}
  .pp-metric-sub {font-size:0.78rem; color:#94a3b8; margin-top:3px;}
  .pp-muted {color:#94a3b8; font-size:0.85rem;}
  .pp-badge {display:inline-block; padding:2px 8px; border-radius:999px; font-size:0.7rem;
    font-weight:700; color:white; letter-spacing:0.03em; white-space:nowrap;
    vertical-align:middle;}
  .pp-badge-outline {display:inline-block; padding:1px 8px; border-radius:999px;
    font-size:0.7rem; font-weight:600; border:1px solid currentColor; white-space:nowrap;}
  .pp-legend {display:flex; flex-direction:column; gap:6px; font-size:0.78rem; color:#cbd5e1;}
  .pp-legend div {display:flex; gap:8px; align-items:flex-start;}
  .pp-notice {border:1px dashed rgba(148,163,184,0.45); border-radius:12px; padding:14px 16px;
    color:#cbd5e1; background:rgba(148,163,184,0.05);}
  .pp-notice b {color:#e2e8f0;}
  .pp-list-item {display:flex; gap:10px; align-items:flex-start; padding:8px 0;
    border-bottom:1px solid rgba(148,163,184,0.12);}
  .pp-list-item:last-child {border-bottom:none;}
  .pp-tech {display:inline-block; margin:0 6px 6px 0; padding:3px 10px; border-radius:8px;
    font-size:0.8rem; background:rgba(99,102,241,0.14); border:1px solid rgba(99,102,241,0.3);
    cursor:help;}
  .pp-code {border:1px solid rgba(148,163,184,0.2); border-radius:12px; overflow:auto;
    background:#0d1117; max-height:560px; font-size:0.8rem;}
  .pp-code table {border-collapse:collapse; width:100%; margin:0;}
  .pp-code td {border:none !important; padding:0 10px; vertical-align:top;}
  .pp-code td.pp-ln {color:#6e7681; text-align:right; user-select:none; width:1%;
    white-space:nowrap; border-right:1px solid rgba(148,163,184,0.15) !important;}
  .pp-code td.pp-src pre {margin:0; padding:0; background:transparent !important;
    color:#e6edf3; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    white-space:pre; line-height:1.5;}
  .pp-code tr.pp-hl td {background:rgba(250,204,21,0.13);}
  .pp-code tr.pp-hl td.pp-ln {color:#facc15; box-shadow:inset 3px 0 0 #facc15;}
  .pp-code-head {display:flex; justify-content:space-between; align-items:center; gap:8px;
    padding:6px 12px; font-size:0.78rem; color:#94a3b8;
    border-bottom:1px solid rgba(148,163,184,0.15); background:rgba(148,163,184,0.06);}
  .pp-diff-add td {background:rgba(34,197,94,0.14);}
  .pp-diff-del td {background:rgba(244,63,94,0.14);}
  .pp-diff-empty td {background:rgba(148,163,184,0.05);}
  .pp-diff-head-before {color:#fda4af;}
  .pp-diff-head-after {color:#86efac;}
  .pp-stepper {display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 10px 0;}
  .pp-step {padding:3px 10px; border-radius:999px; font-size:0.74rem;
    border:1px solid rgba(148,163,184,0.3); color:#94a3b8;}
  .pp-step.done {border-color:#16a34a; color:#86efac;}
  .pp-step.active {border-color:#8b5cf6; color:#ddd6fe; background:rgba(139,92,246,0.15);}
  .pp-step.failed {border-color:#dc2626; color:#fca5a5;}
</style>
"""


def _register_plotly_template() -> None:
    if PLOTLY_TEMPLATE in pio.templates:
        return
    template = go.layout.Template(pio.templates["plotly_dark"])
    template.layout.update(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, system-ui, sans-serif", "color": "#cbd5e1", "size": 12},
        colorway=["#8b5cf6", "#22d3ee", "#f43f5e", "#22c55e", "#f59e0b", "#3b82f6"],
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        xaxis={"gridcolor": "rgba(148,163,184,0.15)", "zerolinecolor": "rgba(148,163,184,0.25)"},
        yaxis={"gridcolor": "rgba(148,163,184,0.15)", "zerolinecolor": "rgba(148,163,184,0.25)"},
        legend={"bgcolor": "rgba(0,0,0,0)"},
        hoverlabel={"bgcolor": "#1e293b", "font": {"color": "#f1f5f9"}},
    )
    pio.templates[PLOTLY_TEMPLATE] = template


def apply_theme() -> None:
    _register_plotly_template()
    st.markdown(_CSS, unsafe_allow_html=True)


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def badge(text: str, color: str, title: str | None = None) -> str:
    tip = f' title="{esc(title)}"' if title else ""
    return f'<span class="pp-badge" style="background:{color}"{tip}>{esc(text)}</span>'


def provenance_badge(provenance: str) -> str:
    return badge(provenance, PROVENANCE_COLORS.get(provenance, "#64748b"))


def severity_badge(severity: str) -> str:
    return badge(severity, SEVERITY_COLORS.get(severity, "#64748b"))


def status_badge(status: str) -> str:
    return badge(status, STATUS_COLORS.get(status, "#64748b"))


def severity_cell_style(value: object) -> str:
    """pandas Styler rule colouring a severity cell."""
    color = SEVERITY_COLORS.get(str(value))
    return f"color:{color};font-weight:700" if color else ""


def plotly_template() -> str:
    _register_plotly_template()
    return PLOTLY_TEMPLATE
