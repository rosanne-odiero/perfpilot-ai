"""Worker jobs for repository optimization candidates (sandbox validation and A/B benchmark)."""

from __future__ import annotations

from typing import Any

from backend.core.exceptions import ValidationFailedError
from backend.jobs.registry import JobContext, job_handler
from backend.optimization.repo_benchmark import execute_repo_benchmark
from backend.optimization.repo_candidates import BENCHMARK_JOB, VALIDATE_JOB
from backend.optimization.repo_validation import execute_repo_validation


def _payload_id(ctx: JobContext, key: str) -> str:
    value = ctx.payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationFailedError(f"payload must contain a string {key!r}")
    return value


@job_handler(VALIDATE_JOB)
def validate_repo_candidate(ctx: JobContext) -> dict[str, Any]:
    ctx.progress("VALIDATING", 0.02, "checking the sandbox and the approved test command")
    result = execute_repo_validation(
        ctx.settings, _payload_id(ctx, "validation_run_id"), ctx.progress
    )
    ctx.progress("COMPLETED", 1.0, f"validation {str(result['status']).lower()}")
    return result


@job_handler(BENCHMARK_JOB)
def benchmark_repo_candidate(ctx: JobContext) -> dict[str, Any]:
    ctx.progress("BENCHMARKING", 0.02, "checking the sandbox and the approved workload command")
    result = execute_repo_benchmark(
        ctx.settings, _payload_id(ctx, "benchmark_id"), ctx.progress, ctx.check_cancelled
    )
    ctx.progress("COMPLETED", 1.0, f"benchmark {str(result['status']).lower()}")
    return result
