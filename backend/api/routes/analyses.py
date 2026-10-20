"""Workload analyses: static rules + measured hotspots -> ranked recommendations."""

from __future__ import annotations

from fastapi import APIRouter, status

from backend.api.deps import DbSession
from backend.api.schemas import JobAccepted
from backend.models.enums import JobStatus
from backend.models.optimization_api import AnalysisOut, AnalysisRequest, RecommendationOut
from backend.services import analyses

router = APIRouter(prefix="/analyses", tags=["optimization"])


@router.post("", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def start_analysis(request: AnalysisRequest, session: DbSession) -> JobAccepted:
    run, job = analyses.submit_analysis(session, request)
    return JobAccepted(
        job_id=job.id, status=JobStatus(job.status), subject_type="analysis_run", subject_id=run.id
    )


@router.get("/{analysis_id}", response_model=AnalysisOut)
def get_analysis(analysis_id: str, session: DbSession) -> AnalysisOut:
    return analyses.analysis_view(session, analysis_id)


@router.get("/{analysis_id}/recommendations", response_model=list[RecommendationOut])
def get_recommendations(analysis_id: str, session: DbSession) -> list[RecommendationOut]:
    return analyses.list_recommendations(session, analysis_id)
