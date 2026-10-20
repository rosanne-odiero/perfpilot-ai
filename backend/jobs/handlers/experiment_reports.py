"""Experiment report generation job (workload optimization candidates and reference benchmarks)."""

from __future__ import annotations

from typing import Any

from backend.jobs.registry import JobContext, job_handler
from backend.reporting.experiment_service import (
    EXPERIMENT_REPORT_JOB_TYPE,
    run_experiment_report_job,
)


@job_handler(EXPERIMENT_REPORT_JOB_TYPE)
def generate_experiment_report(ctx: JobContext) -> dict[str, Any]:
    """Payload ``{"subject_type": "optimization"|"benchmark", "subject_id", "format"}``."""
    return run_experiment_report_job(ctx)
