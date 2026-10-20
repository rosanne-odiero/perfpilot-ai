"""Report export widget shared by audits and workload experiments.

Generates reports in the chosen formats (one job per format), follows the jobs, lists the latest
report per format with a download button, and previews HTML in an isolated frame.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.notices import show_api_error
from frontend.formatting import fmt_dt, human_bytes
from frontend.theme import badge, status_badge

FORMATS = {"html": "HTML", "md": "Markdown", "json": "JSON", "csv": "CSV", "pdf": "PDF"}
MAX_PREVIEW_BYTES = 1_500_000


@dataclass(frozen=True)
class ExportTarget:
    """Where to request reports (``POST {path}``) and how to list them (``GET {path}``)."""

    key: str
    path: str
    empty_message: str
    list_reports: Callable[[], list[dict[str, Any]]] | None = None

    def reports(self) -> list[dict[str, Any]]:
        if self.list_reports is not None:
            return self.list_reports()
        return data.subject_reports(self.path)


def latest_per_format(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for report in sorted(reports, key=lambda r: r["created_at"], reverse=True):
        latest.setdefault(report["format"], report)
    return {fmt: latest[fmt] for fmt in FORMATS if fmt in latest}


def _jobs_key(target: ExportTarget) -> str:
    return f"report_jobs_{target.key}"


def _generate(target: ExportTarget, formats: list[str]) -> None:
    try:
        accepted = api.post(target.path, json={"formats": formats})
    except ApiError as exc:
        show_api_error(exc, "Report request rejected")
        return
    jobs = list(st.session_state.get(_jobs_key(target), []))
    jobs += [r["job_id"] for r in accepted["reports"] if r["job_id"] not in jobs]
    st.session_state[_jobs_key(target)] = jobs
    data.reports.clear()
    data.subject_reports.clear()
    st.rerun()


def _track(target: ExportTarget) -> None:
    remaining = []
    for job_id in st.session_state.get(_jobs_key(target), []):
        job = track_job(job_id, "Generating report")
        if job is None:
            remaining.append(job_id)
        elif not job.succeeded:
            render_job_error(job, "Report generation")
    st.session_state[_jobs_key(target)] = remaining


def _primary(report: dict[str, Any]) -> dict[str, Any] | None:
    return next((f for f in report.get("files", []) if f.get("role") == "primary"), None)


def _downloader(report_id: str) -> Callable[[], bytes]:
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


def render_report_list(reports: list[dict[str, Any]]) -> None:
    with st.container(border=True):
        for report in latest_per_format(reports).values():
            report_row(report)


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


def render_export(target: ExportTarget, blocked: str | None = None) -> None:
    """Format picker, generate button (disabled with ``blocked`` as the reason), jobs, files."""
    left, right = st.columns([3, 1])
    formats = left.pills(
        "Formats",
        list(FORMATS),
        selection_mode="multi",
        default=list(FORMATS),
        format_func=FORMATS.__getitem__,
        key=f"report_formats_{target.key}",
    )
    right.write("")
    if right.button(
        "Generate reports",
        type="primary",
        icon="📄",
        disabled=not formats or blocked is not None,
        help=blocked,
        key=f"report_generate_{target.key}",
    ):
        _generate(target, list(formats or []))
    if blocked:
        st.caption(blocked)
    _track(target)
    reports = target.reports()
    if not reports:
        st.info(target.empty_message)
        return
    render_report_list(reports)
    _preview(list(latest_per_format(reports).values()))
