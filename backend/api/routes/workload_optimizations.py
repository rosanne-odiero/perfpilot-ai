"""Patch candidates of one workload, newest first, so clients can reopen an earlier deep dive."""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from backend.api.deps import DbSession
from backend.models.optimization_api import CandidateOut
from backend.models.orm.optimization import OptimizationCandidate
from backend.services import optimizations
from backend.services.workloads import get_workload

router = APIRouter(tags=["optimization"])


@router.get("/workloads/{workload_id}/optimizations", response_model=list[CandidateOut])
def list_workload_candidates(
    workload_id: str,
    session: DbSession,
    recommendation_id: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=500),
) -> list[CandidateOut]:
    get_workload(session, workload_id)
    stmt = select(OptimizationCandidate.id).where(OptimizationCandidate.workload_id == workload_id)
    if recommendation_id:
        stmt = stmt.where(OptimizationCandidate.recommendation_id == recommendation_id)
    stmt = stmt.order_by(OptimizationCandidate.created_at.desc()).limit(limit)
    return [optimizations.candidate_view(session, cid) for cid in session.scalars(stmt)]
