"""Optimization candidates for findings of imported repositories (Runtime Audit deep dive).

These routes never build or call a sandbox backend (ADR-005): validation and benchmarking are
queued as worker jobs and only reuse commands the user approved for the audit.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, status

from backend.api.deps import AppSettings, DbSession
from backend.api.schemas import JobAccepted
from backend.core.exceptions import NotFoundError
from backend.models.enums import JobStatus
from backend.models.optimization_api import DiffOut
from backend.models.repo_optimization_api import (
    RepoApprovalRequest,
    RepoBenchmarkOut,
    RepoBenchmarkRequest,
    RepoCandidateOut,
    RepoOptimizationRequest,
    RepoRejectionRequest,
    RepoValidationOut,
    RepoValidationRequest,
)
from backend.optimization import repo_candidates as service

router = APIRouter(tags=["repo-optimization"])


@router.post(
    "/audits/{audit_id}/optimizations",
    response_model=RepoCandidateOut,
    status_code=status.HTTP_201_CREATED,
)
def create_repo_optimization(
    audit_id: str, body: RepoOptimizationRequest, session: DbSession, settings: AppSettings
) -> RepoCandidateOut:
    candidate = service.create_candidate(session, settings, audit_id, body.finding_id)
    session.flush()
    return service.candidate_view(session, candidate.id)


@router.get("/audits/{audit_id}/optimizations", response_model=list[RepoCandidateOut])
def list_repo_optimizations(audit_id: str, session: DbSession) -> list[RepoCandidateOut]:
    return service.list_candidates(session, audit_id)


@router.get("/repo-optimizations/{candidate_id}", response_model=RepoCandidateOut)
def get_repo_optimization(candidate_id: str, session: DbSession) -> RepoCandidateOut:
    return service.candidate_view(session, candidate_id)


@router.get("/repo-optimizations/{candidate_id}/diff", response_model=DiffOut)
def get_repo_optimization_diff(candidate_id: str, session: DbSession) -> DiffOut:
    return service.candidate_diff(session, candidate_id)


@router.post("/repo-optimizations/{candidate_id}/approve", response_model=RepoCandidateOut)
def approve_repo_optimization(
    candidate_id: str, body: RepoApprovalRequest, session: DbSession, settings: AppSettings
) -> RepoCandidateOut:
    service.approve_candidate(session, settings, candidate_id, body.approved_by)
    return service.candidate_view(session, candidate_id)


@router.post("/repo-optimizations/{candidate_id}/reject", response_model=RepoCandidateOut)
def reject_repo_optimization(
    candidate_id: str, body: RepoRejectionRequest, session: DbSession, settings: AppSettings
) -> RepoCandidateOut:
    service.reject_candidate(session, settings, candidate_id, body.reason, body.rejected_by)
    return service.candidate_view(session, candidate_id)


@router.post(
    "/repo-optimizations/{candidate_id}/validate",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def validate_repo_optimization(
    candidate_id: str,
    session: DbSession,
    settings: AppSettings,
    body: Annotated[RepoValidationRequest | None, Body()] = None,
) -> JobAccepted:
    request = body or RepoValidationRequest()
    run, job = service.submit_validation(session, settings, candidate_id, request.approval_id)
    return JobAccepted(
        job_id=job.id,
        status=JobStatus(job.status),
        subject_type="repo_validation_run",
        subject_id=run.id,
    )


@router.get("/repo-optimizations/{candidate_id}/validation", response_model=RepoValidationOut)
def get_repo_validation(candidate_id: str, session: DbSession) -> RepoValidationOut:
    candidate = service.get_candidate(session, candidate_id)
    if candidate.validation_run_id is None:
        raise NotFoundError(f"candidate {candidate_id} has not been validated")
    return service.validation_view(session, candidate.validation_run_id)


@router.post(
    "/repo-optimizations/{candidate_id}/benchmark",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def benchmark_repo_optimization(
    candidate_id: str,
    session: DbSession,
    settings: AppSettings,
    body: Annotated[RepoBenchmarkRequest | None, Body()] = None,
) -> JobAccepted:
    run, job = service.submit_benchmark(
        session, settings, candidate_id, body or RepoBenchmarkRequest()
    )
    return JobAccepted(
        job_id=job.id,
        status=JobStatus(job.status),
        subject_type="repo_benchmark_run",
        subject_id=run.id,
    )


@router.get("/repo-optimizations/{candidate_id}/benchmark", response_model=RepoBenchmarkOut)
def get_repo_benchmark(candidate_id: str, session: DbSession) -> RepoBenchmarkOut:
    candidate = service.get_candidate(session, candidate_id)
    if candidate.benchmark_run_id is None:
        raise NotFoundError(f"candidate {candidate_id} has not been benchmarked")
    return service.benchmark_view(session, candidate.benchmark_run_id)
