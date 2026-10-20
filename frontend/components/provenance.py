"""Provenance labels: every result shown in the UI says whether it was measured, derived by
static analysis, or is an AI hypothesis."""

from __future__ import annotations

import streamlit as st

from frontend.theme import PROVENANCE_COLORS, badge

DESCRIPTIONS = {
    "MEASURED": "Observed by running code (profiler, benchmark). Numbers come from real runs.",
    "STATIC": "Derived from reading the code (AST rules, graph). No code was executed.",
    "AI_HYPOTHESIS": "Suggested by a language model. A hypothesis — not measured, not verified.",
}


def provenance_html(provenance: str | None) -> str:
    value = provenance or "STATIC"
    return badge(value, PROVENANCE_COLORS.get(value, "#64748b"), DESCRIPTIONS.get(value))


def legend_html() -> str:
    rows = "".join(
        f"<div>{provenance_html(name)}<span>{text}</span></div>"
        for name, text in DESCRIPTIONS.items()
    )
    return f'<div class="pp-legend">{rows}</div>'


def render_legend() -> None:
    st.markdown(legend_html(), unsafe_allow_html=True)


def render_label(provenance: str | None, note: str | None = None) -> None:
    extra = f' <span class="pp-muted">{note}</span>' if note else ""
    st.markdown(provenance_html(provenance) + extra, unsafe_allow_html=True)
