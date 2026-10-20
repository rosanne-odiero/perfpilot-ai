"""Sidebar: product branding, live API/LLM status and the provenance legend."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from frontend import data
from frontend.api_client import ApiError, base_url
from frontend.components.provenance import render_legend
from frontend.theme import badge

STATIC = Path(__file__).resolve().parent.parent / "static"


def render_sidebar() -> None:
    st.logo(str(STATIC / "logo.svg"), size="large", icon_image=str(STATIC / "icon.svg"))
    with st.sidebar:
        try:
            health = data.health()
        except ApiError as exc:
            st.markdown(badge("API OFFLINE", "#dc2626"), unsafe_allow_html=True)
            st.caption(f"{exc.message}. Start it with `make api` and `make worker`.")
        else:
            api_color = "#16a34a" if health["status"] == "ok" else "#ca8a04"
            llm = health["llm_model"] if health["llm_configured"] else "deterministic mode"
            st.markdown(
                f"{badge('API ' + health['status'].upper(), api_color)} "
                f"{badge('v' + health['version'], '#334155')}",
                unsafe_allow_html=True,
            )
            st.caption(f"LLM: {llm} · {base_url()}")
        with st.expander("Result labels", expanded=False):
            render_legend()
