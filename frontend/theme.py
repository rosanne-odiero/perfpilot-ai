"""Shared visual styling for the Streamlit UI."""

from __future__ import annotations

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

_CSS = """
<style>
  .block-container {padding-top: 1.6rem; max-width: 1400px;}
  .pp-badge {display:inline-block; padding:2px 8px; border-radius:999px; font-size:0.72rem;
             font-weight:600; color:white; letter-spacing:0.02em;}
  .pp-card {border:1px solid rgba(148,163,184,0.25); border-radius:12px; padding:14px 16px;
            background:rgba(148,163,184,0.06); margin-bottom:10px;}
  .pp-muted {color:#94a3b8; font-size:0.85rem;}
  .pp-hero h1 {margin-bottom:0.2rem;}
</style>
"""


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def badge(text: str, color: str) -> str:
    return f'<span class="pp-badge" style="background:{color}">{text}</span>'


def provenance_badge(provenance: str) -> str:
    return badge(provenance, PROVENANCE_COLORS.get(provenance, "#64748b"))


def severity_badge(severity: str) -> str:
    return badge(severity, SEVERITY_COLORS.get(severity, "#64748b"))
