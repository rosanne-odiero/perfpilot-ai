"""Repository Audit API: start static audits and browse findings, graph, and modules."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from backend.api.deps import DbSession
from backend.api.schemas import JobAccepted
from backend.models.audit_schemas import DependencyGraph, Finding
from backend.models.enums import (
    AuditMode,
    AuditStage,
    AuditStatus,
    FindingCategory,
    JobStatus,
    Provenance,
    Severity,
)
from backend.models.orm.audit import AuditRun
from backend.models.orm.platform import Job
from backend.services import audits as service

router = APIRouter(prefix="/audits", tags=["audits"])


class AuditCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=36)
    mode: AuditMode = AuditMode.STATIC
    use_llm: bool = False
    llm_consent: bool = False


class AuditOut(BaseModel):
    id: str
    snapshot_id: str
    mode: AuditMode
    status: AuditStatus
    stage: AuditStage
    use_llm: bool
    llm_consent: bool
    summary: dict[str, Any]
    warnings: list[Any]
    error: str | None
    job_id: str | None
    job_status: JobStatus | None = None
    job_progress: float | None = None
    job_message: str | None = None
    created_at: datetime
    updated_at: datetime


class FindingsPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[Finding]


class ModuleSummary(BaseModel):
    module_name: str
    file_path: str
    is_package: bool
    line_count: int
    is_entrypoint: bool
    entrypoint_reasons: list[str]
    parse_error: str | None
    symbol_count: int
    import_count: int
    internal_import_count: int
    fan_in: int
    fan_out: int
    in_cycle: bool
    finding_count: int


class FileFindingCount(BaseModel):
    file_path: str
    total: int
    by_severity: dict[str, int]
    max_priority: float


def _audit_out(audit: AuditRun, job: Job | None) -> AuditOut:
    return AuditOut(
        id=audit.id,
        snapshot_id=audit.snapshot_id,
        mode=AuditMode(audit.mode),
        status=AuditStatus(audit.status),
        stage=AuditStage(audit.stage),
        use_llm=audit.use_llm,
        llm_consent=audit.llm_consent,
        summary=audit.summary or {},
        warnings=audit.warnings or [],
        error=audit.error,
        job_id=audit.job_id,
        job_status=JobStatus(job.status) if job else None,
        job_progress=job.progress if job else None,
        job_message=job.message if job else None,
        created_at=audit.created_at,
        updated_at=audit.updated_at,
    )


@router.post("", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def start_audit(body: AuditCreate, session: DbSession) -> JobAccepted:
    audit, job = service.create_audit(
        session,
        snapshot_id=body.snapshot_id,
        mode=body.mode,
        use_llm=body.use_llm,
        llm_consent=body.llm_consent,
    )
    return JobAccepted(
        job_id=job.id, status=JobStatus(job.status), subject_type="audit", subject_id=audit.id
    )


@router.get("", response_model=list[AuditOut])
def list_audits(
    session: DbSession,
    snapshot_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[AuditOut]:
    audits = service.list_audits(session, snapshot_id=snapshot_id, limit=limit)
    job_ids = [a.job_id for a in audits if a.job_id]
    jobs = {j.id: j for j in session.scalars(select(Job).where(Job.id.in_(job_ids)))}
    return [_audit_out(a, jobs.get(a.job_id) if a.job_id else None) for a in audits]


@router.get("/{audit_id}", response_model=AuditOut)
def get_audit(audit_id: str, session: DbSession) -> AuditOut:
    audit = service.get_audit(session, audit_id)
    return _audit_out(audit, session.get(Job, audit.job_id) if audit.job_id else None)


@router.get("/{audit_id}/findings", response_model=FindingsPage)
def list_findings(
    audit_id: str,
    session: DbSession,
    severity: Annotated[list[Severity] | None, Query()] = None,
    category: Annotated[list[FindingCategory] | None, Query()] = None,
    rule_id: str | None = None,
    file_path: Annotated[str | None, Query(description="Path prefix")] = None,
    provenance: Provenance | None = None,
    min_priority: Annotated[float | None, Query(ge=0)] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> FindingsPage:
    filters = service.FindingFilters(
        severity=severity or (),
        category=category or (),
        rule_id=rule_id,
        file_path=file_path,
        provenance=provenance,
        min_priority=min_priority,
    )
    total, items = service.query_findings(session, audit_id, filters, limit=limit, offset=offset)
    return FindingsPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/{audit_id}/findings/{finding_id}", response_model=Finding)
def get_finding(audit_id: str, finding_id: str, session: DbSession) -> Finding:
    return service.get_finding(session, audit_id, finding_id)


@router.get("/{audit_id}/graph", response_model=DependencyGraph)
def get_graph(audit_id: str, session: DbSession) -> DependencyGraph:
    return service.require_code_index(service.get_audit(session, audit_id)).graph


@router.get("/{audit_id}/modules", response_model=list[ModuleSummary])
def list_modules(
    audit_id: str,
    session: DbSession,
    path: Annotated[str | None, Query(description="File path prefix")] = None,
) -> list[ModuleSummary]:
    index = service.require_code_index(service.get_audit(session, audit_id))
    nodes = {n.module_name: n for n in index.graph.nodes}
    summaries: list[ModuleSummary] = []
    for module in index.modules:
        if path and not module.file_path.startswith(path):
            continue
        node = nodes.get(module.module_name)
        summaries.append(
            ModuleSummary(
                module_name=module.module_name,
                file_path=module.file_path,
                is_package=module.is_package,
                line_count=module.line_count,
                is_entrypoint=module.is_entrypoint,
                entrypoint_reasons=module.entrypoint_reasons,
                parse_error=module.parse_error,
                symbol_count=len(module.symbols),
                import_count=len(module.imports),
                internal_import_count=sum(1 for i in module.imports if i.is_internal),
                fan_in=node.fan_in if node else 0,
                fan_out=node.fan_out if node else 0,
                in_cycle=node.in_cycle if node else False,
                finding_count=node.finding_count if node else 0,
            )
        )
    return summaries


@router.get("/{audit_id}/files/findings-count", response_model=list[FileFindingCount])
def file_findings_count(audit_id: str, session: DbSession) -> list[FileFindingCount]:
    counts = service.file_finding_counts(session, audit_id)
    return [FileFindingCount(file_path=path, **entry) for path, entry in counts.items()]
