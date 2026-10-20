from __future__ import annotations

from fastapi import APIRouter, Query

from backend.api.deps import DbSession
from backend.api.schemas import JobOut
from backend.jobs import service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(
    session: DbSession,
    limit: int = Query(50, ge=1, le=500),
    job_type: str | None = None,
) -> list[JobOut]:
    return [
        JobOut.model_validate(j) for j in service.list_jobs(session, limit=limit, job_type=job_type)
    ]


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: DbSession) -> JobOut:
    return JobOut.model_validate(service.get_job(session, job_id))


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, session: DbSession) -> JobOut:
    return JobOut.model_validate(service.request_cancel(session, job_id))
