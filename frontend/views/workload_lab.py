"""Workload Lab: the deep-dive for one trusted workload (explore, profile, benchmark, optimize)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import data, routes
from frontend.api_client import ApiError
from frontend.components.notices import empty_state, show_api_error
from frontend.components.url_select import url_selectbox
from frontend.theme import badge, esc
from frontend.views import (
    workload_benchmark,
    workload_explorer,
    workload_optimize,
    workload_profile,
)

SECTIONS = {
    "Explorer": workload_explorer.render,
    "Profile": workload_profile.render,
    "Benchmark": workload_benchmark.render,
    "Optimize": workload_optimize.render,
}
TAB_KEY = routes.LAB_TAB_KEY


def _header(detail: dict[str, Any]) -> None:
    badges = " ".join(
        [
            badge(detail["slug"], "#334155"),
            badge(f"entrypoint {detail['entrypoint']}", "#1e3a8a"),
            badge(f"invocation {detail['invocation_mode']}", "#1e293b"),
            badge("reference implementation bundled", "#4c1d95")
            if detail.get("has_reference_candidate")
            else "",
        ]
    )
    st.markdown(
        f'<div class="pp-hero"><h1>{esc(detail["name"])}</h1>'
        f"<p>{esc(detail['description'])}</p>{badges}</div>",
        unsafe_allow_html=True,
    )


def render_page() -> None:
    try:
        workloads = data.all_workloads()
    except ApiError as exc:
        show_api_error(exc)
        return
    if not workloads:
        empty_state(
            "No workloads are registered.",
            "Bundled examples are seeded when the API starts; check the API logs.",
        )
        return
    labels = {w["id"]: f"{w['name']} ({w['slug']})" for w in workloads}
    left, right = st.columns([2, 3])
    workload_id = url_selectbox(
        "Workload",
        list(labels),
        key="workload_select",
        param=routes.WORKLOAD_PARAM,
        format_func=labels.__getitem__,
        container=left,
    )
    if workload_id is None:
        return
    right.caption(
        "Trusted, bundled workloads: deterministic inputs, profiled and benchmarked in fresh "
        "processes. Results here are MEASURED on this machine."
    )
    from_audit = st.query_params.get("from_audit")
    if from_audit:
        st.info(f"Coming from runtime audit `{from_audit}`.", icon="🧭")
    try:
        detail = data.workload(workload_id)
    except ApiError as exc:
        show_api_error(exc, "Loading workload")
        return
    _header(detail)
    tabs = st.tabs(list(SECTIONS), key=TAB_KEY, on_change="rerun")
    for (name, render), tab in zip(SECTIONS.items(), tabs, strict=True):
        if tab.open:
            with tab:
                try:
                    render(detail)
                except ApiError as exc:
                    show_api_error(exc, name)
