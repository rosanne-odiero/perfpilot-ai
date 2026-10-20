"""Runtime Audit jobs. Only the worker runs these; they are the only code that uses a sandbox."""

from __future__ import annotations

from typing import Any

from backend.core.exceptions import ValidationFailedError
from backend.jobs.registry import JobContext, job_handler
from backend.models.database import session_scope
from backend.runtime.backends import observe_backend, public_details, record_backend_status
from backend.runtime.execution import execute_profile_run
from backend.runtime.service import BACKEND_CHECK_JOB, RUNTIME_PROFILE_JOB


@job_handler(RUNTIME_PROFILE_JOB)
def run_runtime_profile(ctx: JobContext) -> dict[str, Any]:
    run_id = ctx.payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValidationFailedError("payload must contain a string 'run_id'")
    ctx.progress("RUNTIME_PROFILING", 0.05, "checking the sandbox and the approval")
    result = execute_profile_run(ctx.settings, run_id, ctx.check_cancelled)
    ctx.progress("COMPLETED", 1.0, f"run {result['status'].lower()}")
    return result


@job_handler(BACKEND_CHECK_JOB)
def check_runtime_backend(ctx: JobContext) -> dict[str, Any]:
    _, status = observe_backend(ctx.settings)
    with session_scope() as session:
        record_backend_status(session, ctx.settings, status)
    return {
        "backend": status.backend,
        "available": status.available,
        "reason": status.reason,
        "details": public_details(status.details),
    }
