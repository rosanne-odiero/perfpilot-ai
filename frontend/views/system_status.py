"""System status: API, database, LLM configuration, sandbox availability and jobs."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError, Unsupported
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import NOT_AVAILABLE, show_api_error
from frontend.formatting import TERMINAL_JOB_STATES, fmt_dt, fmt_duration, human_bytes


def _capability(payload: Any) -> tuple[str, str | None]:
    if isinstance(payload, Unsupported):
        return "not in this API version", NOT_AVAILABLE
    available = payload.get("available")
    reason = payload.get("reason") or payload.get("detail")
    label = "available" if available else "unavailable" if available is not None else "reported"
    return label, str(reason) if reason else None


def _health_cards(health: dict[str, Any]) -> None:
    runtime, runtime_note = _capability(data.runtime_status())
    ai, ai_note = _capability(data.ai_status())
    render_cards(
        [
            Card(
                "API",
                health["status"].upper(),
                f"version {health['version']}",
                color="#22c55e" if health["status"] == "ok" else "#f59e0b",
            ),
            Card("Database", health["database"]),
            Card(
                "LLM",
                health["llm_model"] if health["llm_configured"] else "not configured",
                "AI suggestions enabled (opt-in per audit)"
                if health["llm_configured"]
                else "deterministic mode; nothing is sent externally",
            ),
            Card(
                "Sandbox backend",
                health["sandbox_backend"],
                f"runtime audit: {runtime}",
                help=runtime_note,
            ),
            Card("AI advisor status", ai, ai_note),
        ]
    )


def _import_options() -> None:
    options = data.import_options()
    if isinstance(options, Unsupported):
        st.caption(f"Import options: {NOT_AVAILABLE}.")
        return
    limits = options["limits"]
    st.dataframe(
        pd.DataFrame(
            [
                {"Setting": "GitHub hosts", "Value": ", ".join(options["github_allowed_hosts"])},
                {
                    "Setting": "Local directory import",
                    "Value": f"enabled ({options['local_allowlist_roots']} allowlisted roots)"
                    if options["local_directory_enabled"]
                    else "disabled",
                },
                {"Setting": "Max upload", "Value": human_bytes(limits["max_upload_bytes"])},
                {"Setting": "Max repository files", "Value": f"{limits['max_repo_files']:,}"},
                {"Setting": "Max repository size", "Value": human_bytes(limits["max_repo_bytes"])},
                {"Setting": "Max file size", "Value": human_bytes(limits["max_file_bytes"])},
                {"Setting": "Max Python files", "Value": f"{limits['max_python_files']:,}"},
                {"Setting": "Clone timeout", "Value": f"{limits['clone_timeout_seconds']} s"},
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def _jobs(health: dict[str, Any]) -> None:
    cols = st.columns([1.5, 1.5, 1])
    job_type = cols[0].selectbox("Job type", ["(all)", *health["job_types"]])
    status = cols[1].selectbox(
        "Status", ["(all)", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"]
    )
    if cols[2].button("Refresh", icon="🔄"):
        data.jobs.clear()
    jobs = data.jobs(None if job_type == "(all)" else job_type, 200)
    if status != "(all)":
        jobs = [j for j in jobs if j["status"] == status]
    if not jobs:
        st.info("No jobs match. Jobs appear when you import, audit, profile or benchmark.")
        return
    frame = pd.DataFrame(
        [
            {
                "Type": j["job_type"],
                "Status": j["status"],
                "Stage": j.get("stage") or "",
                "Progress": float(j.get("progress") or 0.0),
                "Message": j.get("message") or "",
                "Error": j.get("error") or "",
                "Created": fmt_dt(j["created_at"]),
                "Duration": fmt_duration(j.get("started_at"), j.get("finished_at")),
                "Id": j["id"],
            }
            for j in jobs
        ]
    )
    st.dataframe(
        frame,
        hide_index=True,
        width="stretch",
        column_config={
            "Progress": st.column_config.ProgressColumn(
                min_value=0.0, max_value=1.0, format="percent"
            ),
        },
    )
    running = [j for j in jobs if j["status"] not in TERMINAL_JOB_STATES]
    if running:
        target = st.selectbox(
            "Cancel a queued or running job",
            [j["id"] for j in running],
            format_func=lambda i: next(
                f"{j['job_type']} · {i[:8]}" for j in running if j["id"] == i
            ),
        )
        if st.button("Request cancellation", icon="🛑"):
            try:
                api.post(f"/jobs/{target}/cancel")
            except ApiError as exc:
                show_api_error(exc, "Cancel")
            else:
                data.jobs.clear()
                st.rerun()


def render_page() -> None:
    st.title("System status")
    try:
        health = data.health()
    except ApiError as exc:
        show_api_error(exc)
        return
    _health_cards(health)
    left, right = st.columns([1, 2], gap="large")
    with left:
        st.markdown("##### Import sources and limits")
        _import_options()
        st.markdown("##### Registered job types")
        st.write(", ".join(f"`{t}`" for t in health["job_types"]))
    with right:
        st.markdown("##### Jobs")
        _jobs(health)
