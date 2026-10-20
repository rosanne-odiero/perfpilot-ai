"""Runtime tab: discovered commands, explicit approval, run status and MEASURED hotspots."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client as api
from frontend import data, routes
from frontend.api_client import ApiError, Unsupported
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.notices import empty_state, not_available, show_api_error
from frontend.components.provenance import provenance_html
from frontend.formatting import items_of
from frontend.theme import badge, esc
from frontend.views.audit_context import AuditContext

RUN_JOB_KEY = "runtime_job_{audit_id}"


def _status_line() -> dict[str, Any] | None:
    try:
        status = data.runtime_status()
    except ApiError as exc:
        show_api_error(exc, "Runtime status")
        return None
    if isinstance(status, Unsupported):
        return None
    available = bool(status.get("available"))
    reason = status.get("reason") or status.get("detail") or ""
    st.markdown(
        badge(
            "SANDBOX AVAILABLE" if available else "SANDBOX UNAVAILABLE",
            "#16a34a" if available else "#dc2626",
        )
        + (f" <span class='pp-muted'>{esc(reason)}</span>" if reason else ""),
        unsafe_allow_html=True,
    )
    return dict(status)


def _commands_frame(commands: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Kind": c.get("kind", ""),
                "Command": " ".join(map(str, c.get("argv") or [])),
                "Working dir": c.get("workdir_rel", ""),
                "Risk": c.get("estimated_risk", ""),
                "Runnable": bool(c.get("runnable")),
                "Needs dependencies": bool(c.get("requires_dependencies")),
                "Why not runnable": c.get("reason_if_not_runnable") or "",
            }
            for c in commands
        ]
    )


def _approval(
    context: AuditContext, command: dict[str, Any], status: dict[str, Any] | None
) -> None:
    st.markdown("##### Approve this exact command")
    st.caption(
        "Only this argv runs, once, inside the Docker sandbox: no network, read-only source, "
        "resource caps, killed at the time limit. Nothing else is executed."
    )
    st.code(repr(list(command.get("argv") or [])), language="python")
    st.markdown(f"Working directory: `{command.get('workdir_rel') or '.'}`")
    limits = (status or {}).get("limits")
    if isinstance(limits, dict) and limits:
        st.markdown("Sandbox limits reported by the server:")
        st.dataframe(
            pd.DataFrame({"Limit": list(limits), "Value": [str(v) for v in limits.values()]}),
            hide_index=True,
        )
    if command.get("evidence"):
        st.caption("Discovered from: " + ", ".join(f"`{e}`" for e in command["evidence"]))
    if not command.get("runnable"):
        st.warning(command.get("reason_if_not_runnable") or "This command is not runnable.")
        return
    confirmed = st.checkbox(
        "I reviewed the command line and limits above and approve running it once.",
        key=f"approve_{command.get('id')}",
    )
    if st.button("Approve and run", type="primary", disabled=not confirmed, icon="▶️"):
        try:
            accepted = api.post(
                f"/audits/{context.id}/runtime/approvals",
                json={
                    "command_id": command.get("id"),
                    "argv": command.get("argv"),
                    "confirm": True,
                },
            )
        except ApiError as exc:
            show_api_error(exc, "Approval rejected")
            return
        if isinstance(accepted, dict) and accepted.get("job_id"):
            st.session_state[RUN_JOB_KEY.format(audit_id=context.id)] = accepted["job_id"]
        st.cache_data.clear()
        st.rerun()


def _results(context: AuditContext, optimize_available: bool) -> None:
    result = data.audit_optional(context.id, "runtime")
    if isinstance(result, Unsupported) or not isinstance(result, dict) or not result:
        st.caption("No runtime results yet: approve a command above to measure hotspots.")
        return
    st.markdown("##### Measured hotspots " + provenance_html("MEASURED"), unsafe_allow_html=True)
    hotspots = items_of(result.get("hotspots"))
    if hotspots:
        st.dataframe(pd.DataFrame(hotspots), hide_index=True, width="stretch")
        if optimize_available:
            st.page_link(
                routes.WORKLOAD_LAB,
                label="Optimize a hotspot in the Workload Lab (coming from runtime audit)",
                icon="🛠️",
                query_params={"from_audit": context.id},
            )
    else:
        st.caption("The run recorded no hotspots.")
    correlations = items_of(result.get("correlations"))
    if correlations:
        st.markdown("##### Findings confirmed by measurements")
        st.dataframe(pd.DataFrame(correlations), hide_index=True, width="stretch")


def render(context: AuditContext) -> None:
    commands_payload = data.audit_optional(context.id, "runtime/commands")
    if isinstance(commands_payload, Unsupported):
        not_available(
            "Runtime audit",
            "Discovered commands, the approval flow and MEASURED hotspots appear here once the "
            "API serves /api/audits/{id}/runtime/commands.",
        )
        return
    status = _status_line()
    job_id = st.session_state.get(RUN_JOB_KEY.format(audit_id=context.id))
    if job_id:
        job = track_job(job_id, "Runtime run")
        if job is None:
            return
        if not job.succeeded:
            render_job_error(job, "Runtime run")
    commands = items_of(commands_payload)
    if not commands:
        empty_state(
            "No runnable commands were discovered (tests, benchmarks, scripts).",
            "Add a pytest suite or a benchmark script to the repository and re-run the audit.",
        )
    else:
        st.markdown(
            "##### Discovered commands " + provenance_html("STATIC"), unsafe_allow_html=True
        )
        st.caption("Proposals found by reading the repository. None of them has been run.")
        state = st.dataframe(
            _commands_frame(commands),
            key="runtime_commands",
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
        )
        rows = state.selection.rows if state is not None else []
        if rows and rows[0] < len(commands):
            with st.container(border=True):
                _approval(context, commands[rows[0]], status)
    _results(context, data.optimization_api_available())
