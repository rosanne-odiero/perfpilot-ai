"""Optimize tab: the analysis → patch → validation workflow (served by the optimization API)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import data
from frontend.components.notices import not_available


def render(detail: dict[str, Any]) -> None:
    if not data.optimization_api_available():
        not_available(
            "Optimization workflow",
            "Analysis, patch candidates, diff review, approval and validation appear here once "
            "the API serves /api/analyses and /api/optimizations. Until then, benchmark the "
            "bundled reference implementation in the Benchmark tab.",
        )
        return
    st.info(
        f"The optimization API is available on this server, but this UI version does not yet "
        f"drive it for `{detail['slug']}`. Use the API directly (see `/docs`).",
        icon="🛠️",
    )
