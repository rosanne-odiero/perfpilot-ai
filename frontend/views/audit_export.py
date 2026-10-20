"""Export tab: generate reports in every format, follow the jobs, download and preview."""

from __future__ import annotations

import base64
from typing import Any

import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.notices import show_api_error
from frontend.formatting import fmt_dt, human_bytes
from frontend.theme import badge, status_badge
from frontend.views.audit_context import AuditContext

FORMATS = {"html": "HTML", "md": "Markdown", "json": "JSON", "csv": "CSV", "pdf": "PDF"}
JOBS_KEY = "report_jobs_{audit_id}"
MAX_PREVIEW_BYTES = 1_500_000


def _generate(audit_id: str, formats: list[str]) -> None:
    try:
        accepted = api.post(f"/audits/{audit_id}/reports", json={"formats": formats})
    except ApiError as exc:
        show_api_error(exc, "Report request rejected")
        return
    key = JOBS_KEY.format(audit_id=audit_id)
    jobs = list(st.session_state.get(key, []))
    jobs += [r["job_id"] for r in accepted["reports"] if r["job_id"] not in jobs]
    st.session_state[key] = jobs
    data.reports.clear()
    st.rerun()


def _track(audit_id: str) -> None:
    key = JOBS_KEY.format(audit_id=audit_id)
    remaining = []
    for job_id in st.session_state.get(key, []):
        job = track_job(job_id, "Generating report")
        if job is None:
            remaining.append(job_id)
        elif not job.succeeded:
            render_job_error(job, "Report generation")
    st.session_state[key] = remaining


def _primary(report: dict[str, Any]) -> dict[str, Any] | None:
    return next((f for f in report.get("files", []) if f.get("role") == "primary"), None)


def _downloader(report_id: str) -> Any:
    return lambda: api.download(f"/reports/{report_id}/download").content


def report_row(report: dict[str, Any]) -> None:
    primary = _primary(report)
    cols = st.columns([1.1, 2.6, 1.4])
    cols[0].markdown(
        f"{badge(FORMATS.get(report['format'], report['format']).upper(), '#4c1d95')} "
        f"{status_badge(report['status'])}",
        unsafe_allow_html=True,
    )
    if primary:
        engine = report.get("meta", {}).get("pdf_engine")
        extra = f" · rendered with {engine}" if engine else ""
        cols[1].markdown(
            f"`{primary['filename']}` · {human_bytes(primary['size_bytes'])} · "
            f"{fmt_dt(report.get('updated_at'))}{extra}"
        )
        cols[2].download_button(
            "Download",
            data=_downloader(report["id"]),
            file_name=primary["filename"],
            mime=primary["media_type"],
            icon="⬇️",
            key=f"dl_{report['id']}",
            on_click="ignore",
        )
    elif report.get("error"):
        cols[1].error(report["error"])
    else:
        cols[1].caption("Not generated yet.")


def _preview(reports: list[dict[str, Any]]) -> None:
    html_report = next(
        (r for r in reports if r["format"] == "html" and r["status"] == "SUCCEEDED"), None
    )
    if html_report is None:
        return
    with st.expander("Preview the HTML report", expanded=False, icon="👁️"):
        try:
            html = data.report_preview(html_report["id"]).encode("utf-8")
        except ApiError as exc:
            show_api_error(exc, "Preview")
            return
        if len(html) > MAX_PREVIEW_BYTES:
            st.caption("This report is too large to preview inline; download it instead.")
            return
        # A data: URL gives the frame an opaque origin, isolated from the app.
        source = "data:text/html;base64," + base64.b64encode(html).decode("ascii")
        st.iframe(source, height=900)


def render(context: AuditContext) -> None:
    st.markdown("##### Export this audit")
    st.caption(
        "Reports contain the same data as this page, each value with its provenance label. "
        "Secrets are redacted in every format."
    )
    left, right = st.columns([3, 1])
    formats = left.pills(
        "Formats",
        list(FORMATS),
        selection_mode="multi",
        default=list(FORMATS),
        format_func=FORMATS.__getitem__,
        key="report_formats",
    )
    right.write("")
    if right.button("Generate reports", type="primary", icon="📄", disabled=not formats):
        _generate(context.id, list(formats))
    _track(context.id)
    reports = data.reports(context.id)
    if not reports:
        st.info("No reports yet for this audit. Pick formats and press **Generate reports**.")
        return
    latest: dict[str, dict[str, Any]] = {}
    for report in sorted(reports, key=lambda r: r["created_at"], reverse=True):
        latest.setdefault(report["format"], report)
    with st.container(border=True):
        for fmt in FORMATS:
            if fmt in latest:
                report_row(latest[fmt])
    _preview(list(latest.values()))
