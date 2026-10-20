"""Experiment report workflow: request (API) -> ``report.experiment`` job -> stored artifacts.

Reuses the audit report storage: one ``ReportRecord`` per subject + format (``subject_type`` is
``optimization`` or ``benchmark``), artifacts owned by the report, and the generic
``/api/reports/{id}`` metadata, download and preview endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from backend.infrastructure.artifact_store import ArtifactStore
from backend.jobs.registry import JobContext
from backend.jobs.service import enqueue_job
from backend.models.database import session_scope
from backend.models.enums import JobStatus, ReportFormat
from backend.models.orm.audit import ReportRecord
from backend.models.orm.optimization import OptimizationCandidate
from backend.models.orm.platform import Artifact, Job
from backend.models.orm.workloads import BenchmarkRun
from backend.models.workloads import CandidateKind
from backend.reporting.experiment_builder import (
    build_benchmark_report,
    build_optimization_report,
)
from backend.reporting.experiment_model import EXPERIMENT_SCHEMA_VERSION, ExperimentReport
from backend.reporting.renderers.experiment import render_experiment_report
from backend.reporting.service import (
    REPORT_ARTIFACT_KIND,
    REPORT_OWNER_TYPE,
    ReportStatus,
    reconcile_status,
)

EXPERIMENT_REPORT_JOB_TYPE = "report.experiment"
_MAX_RETRIES = 50
_RETRYABLE = {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}
# A report of a candidate while a job is changing it would be stale before it is stored.
_BUSY_CANDIDATE_STATES = {"VALIDATING", "BENCHMARKING"}
_FINISHED_BENCHMARK_STATUSES = {"SUCCEEDED", "FAILED"}


class ExperimentSubject(StrEnum):
    OPTIMIZATION = "optimization"
    BENCHMARK = "benchmark"


def _stamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime("%Y%m%dT%H%M%S%f")


def _subject(session: Session, subject: ExperimentSubject, subject_id: str) -> datetime:
    """Check the subject can be reported on now and return its version (``updated_at``)."""
    if subject == ExperimentSubject.OPTIMIZATION:
        candidate = session.get(OptimizationCandidate, subject_id)
        if candidate is None:
            raise NotFoundError(f"optimization candidate {subject_id} not found")
        if candidate.state in _BUSY_CANDIDATE_STATES:
            raise ConflictError(
                f"candidate {subject_id} is {candidate.state}; request the report once the "
                "running job has finished",
                details={"candidate_id": subject_id, "state": candidate.state},
            )
        return candidate.updated_at
    run = session.get(BenchmarkRun, subject_id)
    if run is None:
        raise NotFoundError(f"benchmark {subject_id} not found")
    if run.candidate_kind != CandidateKind.REFERENCE:
        raise ConflictError(
            f"benchmark {subject_id} measures optimization candidate {run.candidate_id}; "
            "request the experiment report from POST /api/optimizations/{candidate_id}/reports",
            details={"benchmark_id": subject_id, "candidate_id": run.candidate_id},
        )
    if run.status not in _FINISHED_BENCHMARK_STATUSES:
        raise ConflictError(
            f"benchmark {subject_id} is {run.status}; request the report once it has finished",
            details={"benchmark_id": subject_id, "status": run.status},
        )
    return run.updated_at


def idempotency_key(
    subject: ExperimentSubject, subject_id: str, fmt: ReportFormat, version: datetime
) -> str:
    """One report per subject + format + subject version (``updated_at``)."""
    return f"experiment-report:{subject.value}:{subject_id}:{fmt.value}:{_stamp(version)}"


def _new_record(
    session: Session, subject: ExperimentSubject, subject_id: str, fmt: ReportFormat, job_id: str
) -> ReportRecord:
    record = ReportRecord(
        audit_id=None,
        subject_type=subject.value,
        subject_id=subject_id,
        format=fmt.value,
        status=ReportStatus.PENDING,
        job_id=job_id,
    )
    session.add(record)
    session.flush()
    return record


def _request_one(
    session: Session,
    subject: ExperimentSubject,
    subject_id: str,
    fmt: ReportFormat,
    version: datetime,
) -> tuple[ReportRecord, Job]:
    base = idempotency_key(subject, subject_id, fmt, version)
    key = base
    for attempt in range(1, _MAX_RETRIES + 1):
        existing = session.scalar(select(Job).where(Job.idempotency_key == key))
        if existing is None:
            break
        record = session.scalar(select(ReportRecord).where(ReportRecord.job_id == existing.id))
        if JobStatus(existing.status) not in _RETRYABLE:
            return record or _new_record(session, subject, subject_id, fmt, existing.id), existing
        if record is not None:
            reconcile_status(session, record)
        key = f"{base}:r{attempt}"
    else:
        raise ConflictError("too many failed attempts for this report", details={"key": base})
    job = enqueue_job(
        session,
        EXPERIMENT_REPORT_JOB_TYPE,
        {"subject_type": subject.value, "subject_id": subject_id, "format": fmt.value},
        idempotency_key=key,
        subject_type=subject.value,
        subject_id=subject_id,
    )
    return _new_record(session, subject, subject_id, fmt, job.id), job


def request_experiment_reports(
    session: Session, subject: ExperimentSubject, subject_id: str, formats: list[ReportFormat]
) -> list[tuple[ReportRecord, Job]]:
    version = _subject(session, subject, subject_id)
    unique = list(dict.fromkeys(formats))
    if not unique:
        raise ValidationFailedError("at least one report format is required")
    return [_request_one(session, subject, subject_id, fmt, version) for fmt in unique]


def list_experiment_reports(
    session: Session, subject: ExperimentSubject, subject_id: str
) -> list[ReportRecord]:
    model = OptimizationCandidate if subject == ExperimentSubject.OPTIMIZATION else BenchmarkRun
    if session.get(model, subject_id) is None:
        raise NotFoundError(f"{subject.value} {subject_id} not found")
    records = session.scalars(
        select(ReportRecord)
        .where(ReportRecord.subject_type == subject.value, ReportRecord.subject_id == subject_id)
        .order_by(ReportRecord.created_at.desc())
    )
    return [reconcile_status(session, r) for r in records]


def build_experiment_report(
    session: Session, subject: ExperimentSubject, subject_id: str
) -> ExperimentReport:
    if subject == ExperimentSubject.OPTIMIZATION:
        return build_optimization_report(session, subject_id)
    return build_benchmark_report(session, subject_id)


def _set_failed(report_id: str, error: str) -> None:
    with session_scope() as session:
        record = session.get(ReportRecord, report_id)
        if record is not None:
            record.status = ReportStatus.FAILED
            record.error = error[:2000]


def _parse_payload(payload: dict[str, Any]) -> tuple[ExperimentSubject, str, ReportFormat]:
    try:
        subject = ExperimentSubject(str(payload.get("subject_type", "")))
        fmt = ReportFormat(str(payload.get("format", "")))
    except ValueError as exc:
        raise ValidationFailedError(f"invalid experiment report request: {exc}") from exc
    subject_id = str(payload.get("subject_id", ""))
    if not subject_id:
        raise ValidationFailedError("subject_id is required")
    return subject, subject_id, fmt


def run_experiment_report_job(ctx: JobContext) -> dict[str, Any]:
    """Body of the ``report.experiment`` job: build, render, store, and record the outcome."""
    subject, subject_id, fmt = _parse_payload(ctx.payload)
    with session_scope() as session:
        record = session.scalar(select(ReportRecord).where(ReportRecord.job_id == ctx.job_id))
        if record is None:
            record = _new_record(session, subject, subject_id, fmt, ctx.job_id)
        record.status = ReportStatus.RUNNING
        record.error = None
        report_id = record.id
    try:
        ctx.progress("building", 0.1, f"collecting {subject.value} {subject_id}")
        with session_scope() as session:
            report = build_experiment_report(session, subject, subject_id)
        ctx.progress("rendering", 0.4, f"rendering {fmt.value}")
        result = render_experiment_report(report, fmt)
        ctx.progress("storing", 0.9, "storing report")
        store = ArtifactStore(ctx.settings)
        with session_scope() as session:
            stored: list[Artifact] = [
                store.save_bytes(
                    session,
                    kind=REPORT_ARTIFACT_KIND,
                    filename=rendered.filename,
                    data=rendered.data,
                    media_type=rendered.media_type,
                    owner_type=REPORT_OWNER_TYPE,
                    owner_id=report_id,
                    meta={
                        "role": rendered.role,
                        "report_id": report_id,
                        "subject_type": subject.value,
                        "subject_id": subject_id,
                        "experiment_kind": report.kind.value,
                        "experiment_state": report.outcome.state,
                        "format": fmt.value,
                        "schema_version": EXPERIMENT_SCHEMA_VERSION,
                        "generated_at": report.metadata.generated_at.isoformat(),
                        "pdf_engine": result.engine,
                        "redaction_count": result.redaction_count,
                        "redaction_kinds": result.redaction_kinds,
                    },
                )
                for rendered in result.files
            ]
            finished = session.get(ReportRecord, report_id)
            if finished is not None:
                finished.status = ReportStatus.SUCCEEDED
                finished.artifact_id = stored[0].id
                finished.error = None
    except Exception as exc:
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        _set_failed(report_id, str(message))
        raise
    return {
        "report_id": report_id,
        "artifact_id": stored[0].id,
        "subject_type": subject.value,
        "subject_id": subject_id,
        "format": fmt.value,
        "files": [a.filename for a in stored],
        "size_bytes": stored[0].size_bytes,
        "pdf_engine": result.engine,
        "redaction_count": result.redaction_count,
    }
