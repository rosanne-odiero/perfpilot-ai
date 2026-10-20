"""Audit Results page: pick an audit, follow it while running, then browse its sections."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import data, routes
from frontend.api_client import ApiError
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.notices import empty_state, show_api_error
from frontend.formatting import fmt_dt, short_sha
from frontend.theme import badge, esc, status_badge
from frontend.views import (
    audit_ai,
    audit_export,
    audit_files,
    audit_findings,
    audit_graph,
    audit_optimize,
    audit_overview,
    audit_runtime,
)
from frontend.views.audit_context import TAB_KEY, AuditContext
from frontend.views.catalog import AuditRow, audit_row, audit_rows

MODE_LABELS = {"STATIC": "static audit", "RUNTIME": "runtime audit"}
SECTIONS = {
    "Overview": audit_overview.render,
    "Files": audit_files.render,
    "Findings": audit_findings.render,
    "Dependency graph": audit_graph.render,
    "AI insights": audit_ai.render,
    "Runtime": audit_runtime.render,
    audit_optimize.TAB_NAME: audit_optimize.render,
    "Export": audit_export.render,
}


def _select_audit(rows: list[AuditRow]) -> str | None:
    labels = {row.id: row.label for row in rows}
    requested = st.query_params.get(routes.AUDIT_PARAM)
    options = list(labels)
    if requested and requested not in labels:
        options.insert(0, requested)
    return st.selectbox(
        "Audit",
        options,
        format_func=lambda audit_id: labels.get(audit_id, audit_id),
        key=routes.AUDIT_PARAM,
        bind="query-params",
        label_visibility="collapsed",
    )


def _header(row: AuditRow, audit: dict[str, Any]) -> None:
    summary = audit.get("summary") or {}
    commit = (
        badge(f"commit {short_sha(row.commit_sha)}", "#334155", row.commit_sha)
        if row.commit_sha
        else badge("no git metadata", "#334155")
    )
    branch = badge(f"branch {row.branch}", "#334155") if row.branch else ""
    duration = summary.get("duration_seconds")
    took = f" · audit took {duration:.2f} s" if isinstance(duration, int | float) else ""
    st.markdown(
        f"## {esc(row.repo_name)}\n\n"
        f"{status_badge(row.status)} {badge(MODE_LABELS.get(row.mode, row.mode), '#475569')} "
        f"{badge(row.source_label, '#1e3a8a')} {commit} {branch}"
        f"<br><span class='pp-muted'>Audit {esc(row.id)} · started {fmt_dt(row.created_at)}"
        f"{took}</span>",
        unsafe_allow_html=True,
    )


def _render_running(audit: dict[str, Any]) -> bool:
    """Return True when the audit is still running (a live tracker is shown instead)."""
    if audit["status"] not in {"PENDING", "RUNNING"}:
        return False
    job_id = audit.get("job_id")
    if not isinstance(job_id, str):
        st.info("The audit is queued; no job is attached yet.")
        return True
    job = track_job(job_id, "Audit")
    if job is None:
        return True
    if not job.succeeded:
        render_job_error(job, "Audit job")
    else:
        st.info("The audit job finished; its results are being recorded. Refresh in a moment.")
    return True


def render_page() -> None:
    try:
        rows = audit_rows(100)
    except ApiError as exc:
        show_api_error(exc)
        return
    if not rows and not st.query_params.get(routes.AUDIT_PARAM):
        st.title("Audit results")
        empty_state(
            "No audits yet.",
            "Import a repository on the Repository Audit page and start a static audit.",
        )
        st.page_link(routes.REPOSITORY_AUDIT, label="Go to Repository Audit", icon="🔎")
        return
    audit_id = _select_audit(rows)
    if not audit_id:
        return
    try:
        audit = data.audit(audit_id)
        row = audit_row(audit_id)
        snapshot = data.snapshot(row.snapshot_id)
    except ApiError as exc:
        show_api_error(exc, "Loading audit")
        return
    _header(row, audit)
    if _render_running(audit):
        return
    if audit["status"] == "FAILED":
        st.error(
            f"The audit failed: {audit.get('error') or 'no error message recorded'}", icon="⛔"
        )
    elif audit["status"] == "PARTIAL":
        reasons = audit.get("summary", {}).get("partial_reasons") or []
        st.warning(
            "Partial audit: " + ("; ".join(map(str, reasons)) or "some stages did not complete"),
            icon="⚠️",
        )
    context = AuditContext(audit=audit, row=row, snapshot=snapshot)
    tabs = st.tabs(list(SECTIONS), key=TAB_KEY, on_change="rerun")
    for (name, render), tab in zip(SECTIONS.items(), tabs, strict=True):
        if tab.open:
            with tab:
                try:
                    render(context)
                except ApiError as exc:
                    show_api_error(exc, name)
