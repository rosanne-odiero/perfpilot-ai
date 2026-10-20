"""Paired A/B benchmarks of a workload's baseline against a candidate tree."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, status

from backend.api.deps import DbSession
from backend.api.schemas import JobAccepted
from backend.models.enums import JobStatus
from backend.models.workload_api import BenchmarkRunOut, BenchmarkSampleOut
from backend.models.workloads import BenchmarkRequest
from backend.services import benchmarks

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.post("", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def start_benchmark(request: BenchmarkRequest, session: DbSession) -> JobAccepted:
    run, job = benchmarks.submit_benchmark(session, request)
    return JobAccepted(
        job_id=job.id,
        status=JobStatus(job.status),
        subject_type="benchmark_run",
        subject_id=run.id,
    )


@router.get("/{benchmark_id}", response_model=BenchmarkRunOut)
def get_benchmark(benchmark_id: str, session: DbSession) -> BenchmarkRunOut:
    return benchmarks.benchmark_view(session, benchmark_id)


@router.get("/{benchmark_id}/results", response_model=list[BenchmarkSampleOut])
def get_benchmark_results(
    benchmark_id: str,
    session: DbSession,
    kind: Literal["preflight", "timing", "memory"] | None = None,
) -> list[BenchmarkSampleOut]:
    return benchmarks.benchmark_samples(session, benchmark_id, kind=kind)
