"""Report generation workflow: request (API) -> ``report.generate`` job -> stored artifacts."""

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
from backend.models.enums import AuditStatus, JobStatus, ReportFormat
from backend.models.orm.audit import AuditRun, ReportRecord
from backend.models.orm.platform import Artifact, Job
from backend.reporting.builder import REPORTABLE_STATUSES, build_audit_report
from backend.reporting.model import SCHEMA_VERSION
from backend.reporting.renderers import render_report

REPORT_JOB_TYPE = "report.generate"
REPORT_ARTIFACT_KIND = "report"
REPORT_OWNER_TYPE = "report"
PRIMARY_ROLE = "primary"
_MAX_RETRIES = 50
_RETRYABLE = {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.TIMED_OUT}


class ReportStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def get_audit(session: Session, audit_id: str) -> AuditRun:
    audit = session.get(AuditRun, audit_id)
    if audit is None:
        raise NotFoundError(f"audit {audit_id} not found")
    return audit


def ensure_reportable(audit: AuditRun) -> None:
    if audit.status not in REPORTABLE_STATUSES:
        raise ConflictError(
            f"audit {audit.id} is not finished (status {audit.status}); reports can be "
            f"generated once it is {' or '.join(sorted(REPORTABLE_STATUSES))}",
            details={"audit_id": audit.id, "status": audit.status},
        )


def _version_stamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime("%Y%m%dT%H%M%S%f")


def idempotency_key(audit: AuditRun, fmt: ReportFormat) -> str:
    """One report per audit + format + audit version (``updated_at``)."""
    return f"report:{audit.id}:{fmt.value}:{_version_stamp(audit.updated_at)}"


def _job_by_key(session: Session, key: str) -> Job | None:
    return session.scalar(select(Job).where(Job.idempotency_key == key))


def request_report(
    session: Session, audit: AuditRun, fmt: ReportFormat
) -> tuple[ReportRecord, Job]:
    """Return the existing report for this audit version, or enqueue a new one.

    A previous attempt that failed, timed out or was cancelled is retried under a new key.
    """
    base = idempotency_key(audit, fmt)
    key = base
    for attempt in range(1, _MAX_RETRIES + 1):
        existing = _job_by_key(session, key)
        if existing is None:
            break
        if JobStatus(existing.status) not in _RETRYABLE:
            record = session.scalar(select(ReportRecord).where(ReportRecord.job_id == existing.id))
            if record is None:
                record = _new_record(session, audit.id, fmt, existing.id)
            return record, existing
        key = f"{base}:r{attempt}"
    else:
        raise ConflictError("too many failed attempts for this report", details={"key": base})
    job = enqueue_job(
        session,
        REPORT_JOB_TYPE,
        {"audit_id": audit.id, "format": fmt.value},
        idempotency_key=key,
        subject_type="audit",
        subject_id=audit.id,
    )
    return _new_record(session, audit.id, fmt, job.id), job


def _new_record(session: Session, audit_id: str, fmt: ReportFormat, job_id: str) -> ReportRecord:
    record = ReportRecord(
        audit_id=audit_id,
        subject_type="audit",
        subject_id=audit_id,
        format=fmt.value,
        status=ReportStatus.PENDING,
        job_id=job_id,
    )
    session.add(record)
    session.flush()
    return record


def request_reports(
    session: Session, audit_id: str, formats: list[ReportFormat]
) -> list[tuple[ReportRecord, Job]]:
    audit = get_audit(session, audit_id)
    ensure_reportable(audit)
    unique = list(dict.fromkeys(formats))
    if not unique:
        raise ValidationFailedError("at least one report format is required")
    return [request_report(session, audit, fmt) for fmt in unique]


def list_reports(session: Session, audit_id: str) -> list[ReportRecord]:
    get_audit(session, audit_id)
    return list(
        session.scalars(
            select(ReportRecord)
            .where(ReportRecord.audit_id == audit_id)
            .order_by(ReportRecord.created_at.desc())
        )
    )


def get_report(session: Session, report_id: str) -> ReportRecord:
    record = session.get(ReportRecord, report_id)
    if record is None:
        raise NotFoundError(f"report {report_id} not found")
    return record


def report_artifacts(session: Session, record: ReportRecord) -> list[Artifact]:
    artifacts = session.scalars(
        select(Artifact).where(
            Artifact.owner_type == REPORT_OWNER_TYPE, Artifact.owner_id == record.id
        )
    ).all()
    return sorted(artifacts, key=lambda a: (a.meta.get("role") != PRIMARY_ROLE, a.created_at))


def artifact_for_role(session: Session, record: ReportRecord, role: str) -> Artifact:
    if record.status != ReportStatus.SUCCEEDED:
        raise ConflictError(
            f"report {record.id} is not ready (status {record.status})",
            details={"report_id": record.id, "status": record.status},
        )
    for artifact in report_artifacts(session, record):
        if artifact.meta.get("role") == role:
            return artifact
    raise NotFoundError(f"report {record.id} has no {role!r} file")


def _record_for_job(
    session: Session, ctx: JobContext, audit_id: str, fmt: ReportFormat
) -> ReportRecord:
    record = session.scalar(select(ReportRecord).where(ReportRecord.job_id == ctx.job_id))
    return record or _new_record(session, audit_id, fmt, ctx.job_id)


def _set_status(report_id: str, status: ReportStatus, **fields: Any) -> None:
    with session_scope() as session:
        record = session.get(ReportRecord, report_id)
        if record is not None:
            record.status = status
            for key, value in fields.items():
                setattr(record, key, value)


def run_report_job(ctx: JobContext) -> dict[str, Any]:
    """Body of the ``report.generate`` job: build, render, store, and record the outcome."""
    audit_id = str(ctx.payload.get("audit_id", ""))
    try:
        fmt = ReportFormat(str(ctx.payload.get("format", "")))
    except ValueError as exc:
        raise ValidationFailedError(
            f"unsupported report format {ctx.payload.get('format')!r}"
        ) from exc
    with session_scope() as session:
        record = _record_for_job(session, ctx, audit_id, fmt)
        record.status = ReportStatus.RUNNING
        record.error = None
        report_id = record.id
    try:
        ctx.progress("building", 0.1, "collecting audit data")
        with session_scope() as session:
            report = build_audit_report(session, audit_id, ctx.settings)
            audit_status = AuditStatus(get_audit(session, audit_id).status)
        ctx.progress("rendering", 0.4, f"rendering {fmt.value}")
        result = render_report(report, fmt)
        ctx.progress("storing", 0.9, "storing report")
        store = ArtifactStore(ctx.settings)
        with session_scope() as session:
            stored: list[Artifact] = []
            for rendered in result.files:
                stored.append(
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
                            "audit_id": audit_id,
                            "audit_status": audit_status.value,
                            "format": fmt.value,
                            "schema_version": SCHEMA_VERSION,
                            "generated_at": report.metadata.generated_at.isoformat(),
                            "pdf_engine": result.engine,
                            "redaction_count": result.redaction_count,
                            "redaction_kinds": result.redaction_kinds,
                            "providers_applied": report.metadata.providers_applied,
                        },
                    )
                )
            finished = session.get(ReportRecord, report_id)
            if finished is not None:
                finished.status = ReportStatus.SUCCEEDED
                finished.artifact_id = stored[0].id
                finished.error = None
    except Exception as exc:
        message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        _set_status(report_id, ReportStatus.FAILED, error=str(message)[:2000])
        raise
    return {
        "report_id": report_id,
        "artifact_id": stored[0].id,
        "format": fmt.value,
        "files": [a.filename for a in stored],
        "size_bytes": stored[0].size_bytes,
        "pdf_engine": result.engine,
        "redaction_count": result.redaction_count,
    }
