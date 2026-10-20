"""Workload profiling runs (cProfile + tracemalloc + RSS, optional py-spy)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, status

from backend.api.deps import DbSession
from backend.api.schemas import JobAccepted
from backend.models.enums import JobStatus
from backend.models.workload_api import HotspotOut, ProfileRunOut
from backend.models.workloads import HotspotSort, ProfileRequest
from backend.services import profiling

router = APIRouter(prefix="/profiles", tags=["profiling"])


@router.post("", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def start_profile(request: ProfileRequest, session: DbSession) -> JobAccepted:
    run, job = profiling.submit_profile(session, request)
    return JobAccepted(
        job_id=job.id, status=JobStatus(job.status), subject_type="profile_run", subject_id=run.id
    )


@router.get("/{profile_id}", response_model=ProfileRunOut)
def get_profile(profile_id: str, session: DbSession) -> ProfileRunOut:
    return profiling.profile_view(session, profile_id)


@router.get("/{profile_id}/hotspots", response_model=list[HotspotOut])
def get_hotspots(
    profile_id: str,
    session: DbSession,
    sort: HotspotSort = HotspotSort.EXCLUSIVE,
    scope: Literal["user", "all"] = "all",
    limit: int = Query(100, ge=1, le=2000),
) -> list[HotspotOut]:
    return profiling.list_hotspots(
        session, profile_id, sort=sort, user_only=scope == "user", limit=limit
    )
