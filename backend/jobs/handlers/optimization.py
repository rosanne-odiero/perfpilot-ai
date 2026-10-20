"""Job handlers for workload analysis and patch-candidate validation."""

from __future__ import annotations

from typing import Any

from backend.jobs.registry import JobContext, job_handler
from backend.optimization import candidate_resolver
from backend.services.analyses import ANALYSIS_JOB_TYPE, execute_analysis
from backend.services.optimizations import VALIDATE_JOB_TYPE, execute_validation

# Benchmark jobs for {"candidate_id": ...} run in the worker, so the resolver must be installed
# wherever handlers are loaded.
candidate_resolver.install()


@job_handler(ANALYSIS_JOB_TYPE)
def analyze_workload(ctx: JobContext) -> dict[str, Any]:
    run = execute_analysis(str(ctx.payload["analysis_run_id"]), ctx.settings, ctx.progress)
    return {"analysis_run_id": run.id, "state": run.state, "summary": run.summary}


@job_handler(VALIDATE_JOB_TYPE)
def validate_candidate(ctx: JobContext) -> dict[str, Any]:
    run = execute_validation(str(ctx.payload["validation_run_id"]), ctx.settings, ctx.progress)
    return {
        "validation_run_id": run.id,
        "candidate_id": run.candidate_id,
        "status": run.status,
        "summary": run.summary,
    }
