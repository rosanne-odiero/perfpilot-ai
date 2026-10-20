"""Profile tab: run cProfile + tracemalloc + RSS on a workload and inspect the hotspots."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import empty_state, show_api_error
from frontend.components.provenance import provenance_html
from frontend.formatting import fmt_dt, fmt_ms, human_bytes
from frontend.theme import plotly_template, status_badge
from frontend.views.run_history import runs

JOB_KEY = "profile_job_{workload_id}"
SELECT_KEY = "profile_run_{workload_id}"
SORTS = {"exclusive": "Exclusive (self) time", "cumulative": "Cumulative time", "calls": "Calls"}

EXPLANATION = (
    "**Exclusive** (self) time is spent in the function's own body; **cumulative** time also "
    "includes everything it calls. Exclusive percentages add up to about 100%; cumulative "
    "percentages overlap (a caller includes its callees) and must never be summed."
)


def _start_form(detail: dict[str, Any]) -> None:
    workload_id = detail["id"]
    with st.form(f"profile_form_{workload_id}", border=True):
        cols = st.columns([1, 2, 1])
        sizes = detail["sizes"]
        size = cols[0].selectbox("Input size", sizes, index=sizes.index(detail["default_size"]))
        sampling = cols[1].checkbox(
            "Also sample with py-spy (if installed)",
            help="Optional; skipped with a warning when py-spy is unavailable.",
        )
        submitted = cols[2].form_submit_button("Profile", type="primary", icon="⏱️")
    if not submitted:
        return
    try:
        accepted = api.post(
            "/profiles",
            json={"workload_id": workload_id, "size": size, "enable_sampling": sampling},
        )
    except ApiError as exc:
        show_api_error(exc, "Profile request rejected")
        return
    st.session_state[JOB_KEY.format(workload_id=workload_id)] = accepted["job_id"]
    st.session_state[SELECT_KEY.format(workload_id=workload_id)] = accepted["subject_id"]
    data.jobs.clear()
    st.rerun()


def _hotspot_frame(hotspots: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "#": h["rank"],
                "Function": h["function_name"],
                "Location": f"{h['file_path']}:{h['line_number']}"
                if h["line_number"]
                else h["file_path"],
                "Calls": f"{h['total_calls']}"
                + (f"/{h['primitive_calls']}" if h["primitive_calls"] != h["total_calls"] else ""),
                "Exclusive ms": h["internal_time_ms"],
                "Exclusive %": h["percentage_of_profiled_time"],
                "Cumulative ms": h["cumulative_time_ms"],
                "Cumulative %": h["cumulative_percentage"],
                "User code": h["is_user_code"],
            }
            for h in hotspots
        ]
    )


def _hotspot_chart(hotspots: list[dict[str, Any]], sort: str) -> go.Figure:
    key = "cumulative_time_ms" if sort == "cumulative" else "internal_time_ms"
    top = hotspots[:12]
    figure = go.Figure(
        go.Bar(
            x=[h[key] for h in top],
            y=[h["function_name"][:60] for h in top],
            orientation="h",
            marker_color=["#8b5cf6" if h["is_user_code"] else "#475569" for h in top],
            hovertemplate="%{y}: %{x:.4g} ms<extra></extra>",
        )
    )
    figure.update_layout(
        template=plotly_template(),
        height=max(260, 26 * len(top) + 70),
        title={
            "text": ("Cumulative" if key == "cumulative_time_ms" else "Exclusive")
            + " time (ms) · purple = user code",
            "font": {"size": 13},
        },
        yaxis={"autorange": "reversed"},
    )
    return figure


def _memory(profile: dict[str, Any]) -> None:
    memory = profile.get("memory") or {}
    definitions = memory.get("definitions") or {}
    left, right = st.columns(2)
    with left:
        render_cards(
            [
                Card(
                    "tracemalloc peak",
                    human_bytes(memory.get("tracemalloc_peak_bytes")),
                    f"retained {human_bytes(memory.get('tracemalloc_retained_bytes'))}",
                    "MEASURED",
                    help=definitions.get("tracemalloc_peak_bytes"),
                )
            ]
        )
        if definitions.get("tracemalloc_peak_bytes"):
            st.caption("tracemalloc: " + definitions["tracemalloc_peak_bytes"])
    with right:
        render_cards(
            [
                Card(
                    "RSS peak (process)",
                    human_bytes(memory.get("rss_peak_bytes")),
                    f"ru_maxrss {human_bytes(memory.get('ru_maxrss_bytes'))} · "
                    f"{memory.get('rss_samples', 0)} samples",
                    "MEASURED",
                    help=definitions.get("rss_peak_bytes"),
                )
            ]
        )
        if definitions.get("rss_peak_bytes"):
            st.caption("RSS: " + definitions["rss_peak_bytes"])
    if memory.get("unavailable"):
        st.caption("Unavailable on this platform: " + ", ".join(map(str, memory["unavailable"])))


def _show(profile: dict[str, Any]) -> None:
    if profile["status"] in {"PENDING", "QUEUED", "RUNNING"}:
        st.info("This profile is still running; results appear when it finishes.")
        return
    if profile["status"] != "SUCCEEDED":
        st.error(f"Profile {profile['status'].lower()}: {profile.get('error') or 'no details'}")
        if profile.get("error_details"):
            st.json(profile["error_details"])
        return
    metrics = profile.get("metrics") or {}
    render_cards(
        [
            Card(
                "Profiled time",
                fmt_ms(profile.get("total_profiled_time_ms")),
                "sum of exclusive time",
                "MEASURED",
            ),
            Card(
                "Wall time (under profiler)",
                fmt_ms(profile.get("wall_time_ms")),
                f"CPU {fmt_ms(metrics.get('cpu_time_under_profiler_ms'))}",
                "MEASURED",
            ),
            Card(
                "Functions",
                str(metrics.get("function_count", "—")),
                f"{metrics.get('user_function_count', '—')} in user code",
                "MEASURED",
            ),
            Card(
                "Input size", profile["size"], f"input sha {str(profile.get('input_sha256'))[:10]}"
            ),
        ]
    )
    st.info(EXPLANATION, icon="📐")
    cols = st.columns([2, 1, 1])
    sort = cols[0].radio(
        "Sort hotspots by", list(SORTS), format_func=SORTS.__getitem__, horizontal=True
    )
    scope = cols[1].radio(
        "Scope", ["all", "user"], horizontal=True, format_func=lambda s: s + " code"
    )
    limit = cols[2].number_input("Rows", 5, 500, 50, 5)
    hotspots = data.hotspots(profile["id"], sort, scope, int(limit))
    if not hotspots:
        st.caption("No hotspots recorded for this scope.")
    else:
        left, right = st.columns([1.6, 1], gap="large")
        left.dataframe(
            _hotspot_frame(hotspots),
            hide_index=True,
            width="stretch",
            height=min(420, 38 + 35 * len(hotspots)),
            column_config={
                "Function": st.column_config.TextColumn(width="large"),
                "Exclusive ms": st.column_config.NumberColumn(format="%.4f"),
                "Cumulative ms": st.column_config.NumberColumn(format="%.4f"),
                "Exclusive %": st.column_config.ProgressColumn(
                    format="%.1f%%", min_value=0, max_value=100
                ),
                "Cumulative %": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
        right.plotly_chart(_hotspot_chart(hotspots, sort), theme=None)
    st.markdown("##### Memory " + provenance_html("MEASURED"), unsafe_allow_html=True)
    _memory(profile)
    for warning in profile.get("warnings") or []:
        st.warning(str(warning), icon="⚠️")
    sampling = profile.get("sampling") or {}
    if sampling.get("requested"):
        st.caption(f"py-spy sampling: {sampling}")
    with st.expander("Environment fingerprint and limits"):
        st.json({"environment": profile.get("environment"), "limits": metrics.get("limits")})


def _run_label(ref_run: dict[str, Any], created: str) -> str:
    total = fmt_ms(ref_run.get("total_profiled_time_ms"))
    return (
        f"{fmt_dt(created)} · {ref_run.get('size')} · {ref_run.get('status', '').lower()} · {total}"
    )


def render(detail: dict[str, Any]) -> None:
    workload_id = detail["id"]
    st.markdown(
        "Profile the workload in a fresh, resource-limited process " + provenance_html("MEASURED"),
        unsafe_allow_html=True,
    )
    _start_form(detail)
    job_key = JOB_KEY.format(workload_id=workload_id)
    if job_key in st.session_state:
        job = track_job(st.session_state[job_key], "Profiling")
        if job is None:
            return
        st.session_state.pop(job_key)
        if not job.succeeded:
            render_job_error(job, "Profiling job")
    history = runs("profile", workload_id)
    if not history:
        empty_state("No profiles yet for this workload.", "Choose an input size and press Profile.")
        return
    labels = {ref.id: _run_label(ref.run, ref.created_at) for ref in history}
    select_key = SELECT_KEY.format(workload_id=workload_id)
    if st.session_state.get(select_key) not in labels:
        st.session_state[select_key] = history[0].id
    profile_id = st.selectbox(
        "Profile run", list(labels), format_func=labels.__getitem__, key=select_key
    )
    run = next(ref.run for ref in history if ref.id == profile_id)
    st.markdown(status_badge(run["status"]), unsafe_allow_html=True)
    _show(run)
