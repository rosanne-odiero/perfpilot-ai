"""Report generation job."""

from __future__ import annotations

from typing import Any

from backend.jobs.registry import JobContext, job_handler
from backend.reporting.service import REPORT_JOB_TYPE, run_report_job


@job_handler(REPORT_JOB_TYPE)
def generate_report(ctx: JobContext) -> dict[str, Any]:
    """Payload ``{"audit_id": str, "format": "html"|"md"|"json"|"csv"|"pdf"}``."""
    return run_report_job(ctx)
