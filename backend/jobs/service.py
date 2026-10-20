"""Job queue operations used by the API and services."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.exceptions import ConflictError, NotFoundError
from backend.models.database import utcnow
from backend.models.enums import JobStatus
from backend.models.orm.platform import Job


def enqueue_job(
    session: Session,
    job_type: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str | None = None,
    timeout_seconds: int | None = None,
    max_attempts: int | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> Job:
    """Create a QUEUED job. Re-submitting the same idempotency key returns the original job,
    and conflicting payloads for one key are rejected so duplicates cannot diverge."""
    settings = get_settings()
    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.job_type != job_type or existing.payload != payload:
                raise ConflictError(
                    "idempotency key reused with a different request",
                    details={"job_id": existing.id},
                )
            return existing
    job = Job(
        job_type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
        timeout_seconds=timeout_seconds or settings.job_default_timeout_seconds,
        max_attempts=max_attempts or settings.job_max_attempts,
        subject_type=subject_type,
        subject_id=subject_id,
        status=JobStatus.QUEUED,
    )
    session.add(job)
    session.flush()
    return job


def get_job(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} not found")
    return job


def request_cancel(session: Session, job_id: str) -> Job:
    job = get_job(session, job_id)
    if JobStatus(job.status).is_terminal:
        return job
    if job.status == JobStatus.QUEUED:
        job.status = JobStatus.CANCELLED
        job.finished_at = utcnow()
        job.message = "cancelled before start"
    else:
        job.cancel_requested = True
    session.flush()
    return job


def list_jobs(session: Session, *, limit: int = 50, job_type: str | None = None) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if job_type:
        stmt = stmt.where(Job.job_type == job_type)
    return list(session.scalars(stmt))
