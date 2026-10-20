"""Export tab: generate audit reports in every format, follow the jobs, download and preview."""

from __future__ import annotations

import streamlit as st

from frontend import data
from frontend.components.report_export import ExportTarget, render_export
from frontend.views.audit_context import AuditContext


def render(context: AuditContext) -> None:
    st.markdown("##### Export this audit")
    st.caption(
        "Reports contain the same data as this page, each value with its provenance label. "
        "Secrets are redacted in every format."
    )
    render_export(
        ExportTarget(
            key=f"audit_{context.id}",
            path=f"/audits/{context.id}/reports",
            empty_message="No reports yet for this audit. Pick formats and press "
            "**Generate reports**.",
            list_reports=lambda: data.reports(context.id),
        )
    )
