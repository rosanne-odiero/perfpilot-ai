"""Workload experiment reports and history.

An experiment is one optimization candidate (``/optimizations/{id}/reports``) or one benchmark of
a workload's bundled reference implementation (``/benchmarks/{id}/reports``). Metadata, downloads
and previews of the generated files use the generic ``/reports/{report_id}`` endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status
from pydantic import BaseModel

from backend.api.deps import DbSession
from backend.api.routes.reports import ReportJobAccepted, ReportOut, ReportRequest, _report_out
from backend.models.enums import JobStatus, ReportFormat
from backend.reporting.experiment_service import (
    ExperimentSubject,
    list_experiment_reports,
    request_experiment_reports,
)
from backend.services.experiments import ExperimentKindName, ExperimentOut, list_experiments

router = APIRouter(tags=["experiments"])


class ExperimentReportsAccepted(BaseModel):
    subject_type: ExperimentSubject
    subject_id: str
    reports: list[ReportJobAccepted]


def _accepted(
    session: DbSession, subject: ExperimentSubject, subject_id: str, body: ReportRequest
) -> ExperimentReportsAccepted:
    pairs = request_experiment_reports(session, subject, subject_id, body.formats)
    return ExperimentReportsAccepted(
        subject_type=subject,
        subject_id=subject_id,
        reports=[
            ReportJobAccepted(
                report_id=record.id,
                job_id=job.id,
                format=ReportFormat(record.format),
                status=JobStatus(job.status),
                report_status=record.status,
            )
            for record, job in pairs
        ],
    )


@router.post(
    "/optimizations/{candidate_id}/reports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExperimentReportsAccepted,
)
def request_optimization_reports(
    candidate_id: str, body: ReportRequest, session: DbSession
) -> ExperimentReportsAccepted:
    """One ``report.experiment`` job per format; repeating the request for the same candidate
    version returns the existing reports."""
    return _accepted(session, ExperimentSubject.OPTIMIZATION, candidate_id, body)


@router.get("/optimizations/{candidate_id}/reports", response_model=list[ReportOut])
def list_optimization_reports(candidate_id: str, session: DbSession) -> list[ReportOut]:
    records = list_experiment_reports(session, ExperimentSubject.OPTIMIZATION, candidate_id)
    return [_report_out(session, r) for r in records]


@router.post(
    "/benchmarks/{benchmark_id}/reports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExperimentReportsAccepted,
)
def request_benchmark_reports(
    benchmark_id: str, body: ReportRequest, session: DbSession
) -> ExperimentReportsAccepted:
    """Reports for a finished benchmark of the bundled reference implementation."""
    return _accepted(session, ExperimentSubject.BENCHMARK, benchmark_id, body)


@router.get("/benchmarks/{benchmark_id}/reports", response_model=list[ReportOut])
def list_benchmark_reports(benchmark_id: str, session: DbSession) -> list[ReportOut]:
    records = list_experiment_reports(session, ExperimentSubject.BENCHMARK, benchmark_id)
    return [_report_out(session, r) for r in records]


@router.get("/experiments", response_model=list[ExperimentOut])
def experiment_history(
    session: DbSession,
    limit: int = Query(50, ge=1, le=200),
    workload_id: str | None = Query(None, max_length=64),
    kind: ExperimentKindName | None = None,
) -> list[ExperimentOut]:
    """Recent optimization candidates and reference benchmarks, newest first."""
    return list_experiments(session, limit=limit, workload_id=workload_id, kind=kind)
