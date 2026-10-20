"""Patch candidates: generate, review the diff, approve/reject, validate.

Benchmarking a VALIDATED candidate goes through ``POST /api/benchmarks`` with
``{"candidate": {"candidate_id": ...}}``; the resolver installed here makes that possible.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from backend.api.deps import AppSettings, DbSession
from backend.api.schemas import JobAccepted
from backend.core.exceptions import NotFoundError
from backend.models.enums import JobStatus
from backend.models.optimization_api import (
    ApprovalRequest,
    CandidateOut,
    DiffOut,
    OptimizationRequest,
    RejectionRequest,
    ValidationRunOut,
)
from backend.optimization import candidate_resolver
from backend.services import optimizations

candidate_resolver.install()

router = APIRouter(prefix="/optimizations", tags=["optimization"])


@router.post("", response_model=CandidateOut, status_code=status.HTTP_201_CREATED)
def create_optimization(
    request: OptimizationRequest, session: DbSession, settings: AppSettings
) -> CandidateOut:
    candidate = optimizations.create_candidate(session, settings, request)
    return optimizations.candidate_view(session, candidate.id)


@router.get("/{candidate_id}", response_model=CandidateOut)
def get_optimization(candidate_id: str, session: DbSession) -> CandidateOut:
    return optimizations.candidate_view(session, candidate_id)


@router.get("/{candidate_id}/diff", response_model=DiffOut)
def get_diff(candidate_id: str, session: DbSession) -> DiffOut:
    return optimizations.candidate_diff(session, candidate_id)


@router.post("/{candidate_id}/approve", response_model=CandidateOut)
def approve(candidate_id: str, request: ApprovalRequest, session: DbSession) -> CandidateOut:
    optimizations.approve_candidate(session, candidate_id, request.approved_by)
    return optimizations.candidate_view(session, candidate_id)


@router.post("/{candidate_id}/reject", response_model=CandidateOut)
def reject(candidate_id: str, request: RejectionRequest, session: DbSession) -> CandidateOut:
    optimizations.reject_candidate(session, candidate_id, request.reason, request.rejected_by)
    return optimizations.candidate_view(session, candidate_id)


@router.post(
    "/{candidate_id}/validate", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def validate(candidate_id: str, session: DbSession) -> JobAccepted:
    run, job = optimizations.submit_validation(session, candidate_id)
    return JobAccepted(
        job_id=job.id,
        status=JobStatus(job.status),
        subject_type="validation_run",
        subject_id=run.id,
    )


@router.get("/{candidate_id}/validation", response_model=ValidationRunOut)
def get_validation(candidate_id: str, session: DbSession) -> ValidationRunOut:
    candidate = optimizations.get_candidate(session, candidate_id)
    if candidate.validation_run_id is None:
        raise NotFoundError(f"candidate {candidate_id} has not been validated")
    return optimizations.validation_view(session, candidate.validation_run_id)
