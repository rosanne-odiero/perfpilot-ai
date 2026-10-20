"""Job handlers for workload profiling and A/B benchmarking."""

from __future__ import annotations

from typing import Any

from backend.jobs.registry import JobContext, job_handler
from backend.services.benchmarks import BENCHMARK_JOB_TYPE, execute_benchmark_run
from backend.services.profiling import PROFILE_JOB_TYPE, execute_profile_run


@job_handler(PROFILE_JOB_TYPE)
def profile_workload(ctx: JobContext) -> dict[str, Any]:
    run = execute_profile_run(str(ctx.payload["profile_run_id"]), ctx.settings, ctx.progress)
    return {
        "profile_run_id": run.id,
        "status": run.status,
        "total_profiled_time_ms": run.total_profiled_time_ms,
    }


@job_handler(BENCHMARK_JOB_TYPE)
def benchmark_workload(ctx: JobContext) -> dict[str, Any]:
    run = execute_benchmark_run(str(ctx.payload["benchmark_run_id"]), ctx.settings, ctx.progress)
    return {
        "benchmark_run_id": run.id,
        "status": run.status,
        "classification": run.classification,
        "speedup": run.speedup,
    }
