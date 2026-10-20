"""Runtime Audit API: sandbox status, discovered commands, explicit approvals, and results.

These routes never build or call a sandbox backend (ADR-005). Approving a command queues the
``runtime.profile`` job; the worker runs it in the sandbox.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from backend.api.deps import AppSettings, DbSession
from backend.api.schemas import JobAccepted
from backend.models.enums import JobStatus
from backend.runtime import service
from backend.runtime.backends import latest_backend_status
from backend.runtime.schemas import (
    ApprovalRequest,
    RunDetailOut,
    RuntimeAuditOut,
    RuntimeCommandOut,
    RuntimeLimitsOut,
    RuntimeStatusOut,
)
from backend.sandbox.backend import SandboxLimits

router = APIRouter(tags=["runtime"])


@router.get("/runtime/status", response_model=RuntimeStatusOut)
def runtime_status(session: DbSession, settings: AppSettings) -> RuntimeStatusOut:
    observed = latest_backend_status(session, settings)
    limits = SandboxLimits.from_settings(settings)
    return RuntimeStatusOut(
        backend=observed.backend,
        available=observed.available,
        reason=observed.reason,
        image=settings.sandbox_image,
        default_limits=RuntimeLimitsOut(**limits.model_dump()),
        dependency_install_timeout_seconds=settings.sandbox_install_timeout_seconds,
        approval_ttl_seconds=settings.sandbox_approval_ttl_seconds,
        details=observed.details,
        checked_at=observed.checked_at,
    )


@router.post(
    "/runtime/status/refresh", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED
)
def refresh_runtime_status(session: DbSession) -> JobAccepted:
    job = service.request_backend_check(session)
    return JobAccepted(job_id=job.id, status=JobStatus(job.status), subject_type="runtime")


@router.get("/audits/{audit_id}/runtime/commands", response_model=list[RuntimeCommandOut])
def list_runtime_commands(audit_id: str, session: DbSession) -> list[RuntimeCommandOut]:
    return service.list_commands(session, audit_id)


@router.post(
    "/audits/{audit_id}/runtime/approvals",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def approve_runtime_command(
    audit_id: str, body: ApprovalRequest, session: DbSession, settings: AppSettings
) -> JobAccepted:
    run, job = service.request_run(session, settings, audit_id, body)
    return JobAccepted(
        job_id=job.id, status=JobStatus(job.status), subject_type="runtime_run", subject_id=run.id
    )


@router.get("/audits/{audit_id}/runtime", response_model=RuntimeAuditOut)
def get_runtime_audit(audit_id: str, session: DbSession, settings: AppSettings) -> RuntimeAuditOut:
    return service.runtime_overview(session, settings, audit_id)


@router.get("/audits/{audit_id}/runtime/runs/{run_id}", response_model=RunDetailOut)
def get_runtime_run(audit_id: str, run_id: str, session: DbSession) -> RunDetailOut:
    return service.run_detail(session, audit_id, run_id)
