"""Audit persistence and queries shared by the API routes and the ``audit.run`` job."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.exceptions import (
    ConflictError,
    NotConfiguredError,
    NotFoundError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.jobs.service import enqueue_job
from backend.models.audit_schemas import CodeIndex, Finding
from backend.models.enums import (
    AuditMode,
    AuditStage,
    AuditStatus,
    FindingCategory,
    Provenance,
    Severity,
    SnapshotStatus,
)
from backend.models.orm.audit import AuditRun, FindingRecord, RepositorySnapshot
from backend.models.orm.platform import Job

AUDIT_JOB_TYPE = "audit.run"
_FINDING_NAMESPACE = uuid.UUID("6f1e2c1a-3b7d-4c55-9a51-0f4f3d6f2a10")
RUNTIME_NOT_CONFIGURED = (
    "Runtime audits execute repository code and require the isolated sandbox backend, which is "
    "not available in this build. Start a STATIC audit instead."
)


def finding_record_id(audit_id: str, finding_id: str) -> str:
    """Primary key of a finding row: unique per audit, derived from the stable finding id."""
    return str(uuid.uuid5(_FINDING_NAMESPACE, f"{audit_id}:{finding_id}"))


def get_snapshot(session: Session, snapshot_id: str) -> RepositorySnapshot:
    snapshot = session.get(RepositorySnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError(f"snapshot {snapshot_id} not found")
    return snapshot


def get_audit(session: Session, audit_id: str) -> AuditRun:
    audit = session.get(AuditRun, audit_id)
    if audit is None:
        raise NotFoundError(f"audit {audit_id} not found")
    return audit


def ensure_snapshot_ready(snapshot: RepositorySnapshot) -> tuple[str, dict[str, Any]]:
    """Return (workspace path, inventory JSON) of a READY snapshot or raise ConflictError."""
    if snapshot.status != SnapshotStatus.READY:
        raise ConflictError(
            f"snapshot {snapshot.id} is not ready (status {snapshot.status})",
            details={"snapshot_status": snapshot.status},
        )
    if not snapshot.workspace_path or snapshot.inventory is None:
        raise ConflictError(f"snapshot {snapshot.id} has no workspace or inventory")
    return snapshot.workspace_path, snapshot.inventory


def resolve_workspace(settings: Settings, workspace_path: str) -> Path:
    """Contain the snapshot workspace inside the configured workspaces directory."""
    base = settings.workspaces_dir.resolve()
    candidate = Path(workspace_path)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise SecurityViolationError("snapshot workspace is outside the workspaces directory")
    if not resolved.is_dir():
        raise ConflictError("snapshot workspace no longer exists")
    return resolved


def create_audit(
    session: Session,
    *,
    snapshot_id: str,
    mode: AuditMode,
    use_llm: bool,
    llm_consent: bool,
) -> tuple[AuditRun, Job]:
    if mode is AuditMode.RUNTIME:
        raise NotConfiguredError(RUNTIME_NOT_CONFIGURED, details={"mode": mode.value})
    if use_llm and not llm_consent:
        raise ValidationFailedError(
            "use_llm requires llm_consent=true because code snippets would leave this machine"
        )
    snapshot = get_snapshot(session, snapshot_id)
    ensure_snapshot_ready(snapshot)
    audit = AuditRun(
        snapshot_id=snapshot.id,
        mode=mode.value,
        status=AuditStatus.PENDING,
        stage=AuditStage.QUEUED,
        use_llm=use_llm,
        llm_consent=llm_consent,
        warnings=[],
    )
    session.add(audit)
    session.flush()
    job = enqueue_job(
        session,
        AUDIT_JOB_TYPE,
        {"audit_id": audit.id},
        idempotency_key=f"{AUDIT_JOB_TYPE}:{audit.id}",
        subject_type="audit",
        subject_id=audit.id,
    )
    audit.job_id = job.id
    session.flush()
    return audit, job


def list_audits(session: Session, *, snapshot_id: str | None, limit: int) -> list[AuditRun]:
    stmt = select(AuditRun).order_by(AuditRun.created_at.desc()).limit(limit)
    if snapshot_id:
        stmt = stmt.where(AuditRun.snapshot_id == snapshot_id)
    return list(session.scalars(stmt))


def replace_findings(session: Session, audit_id: str, findings: Sequence[Finding]) -> None:
    for record in session.scalars(select(FindingRecord).where(FindingRecord.audit_id == audit_id)):
        session.delete(record)
    session.flush()
    session.add_all(
        FindingRecord(
            id=finding_record_id(audit_id, finding.id),
            audit_id=audit_id,
            rule_id=finding.rule_id,
            category=finding.category.value,
            severity=finding.severity.value,
            confidence=finding.confidence.value,
            provenance=finding.provenance.value,
            file_path=finding.location.file_path,
            line_start=finding.location.line_start,
            priority_score=finding.priority_score,
            data=finding.model_dump(mode="json"),
        )
        for finding in findings
    )


@dataclass(frozen=True)
class FindingFilters:
    severity: Sequence[Severity] = ()
    category: Sequence[FindingCategory] = ()
    rule_id: str | None = None
    file_path: str | None = None
    provenance: Provenance | None = None
    min_priority: float | None = None


def query_findings(
    session: Session, audit_id: str, filters: FindingFilters, *, limit: int, offset: int
) -> tuple[int, list[Finding]]:
    get_audit(session, audit_id)
    conditions: list[Any] = [FindingRecord.audit_id == audit_id]
    if filters.severity:
        conditions.append(FindingRecord.severity.in_([s.value for s in filters.severity]))
    if filters.category:
        conditions.append(FindingRecord.category.in_([c.value for c in filters.category]))
    if filters.rule_id:
        conditions.append(FindingRecord.rule_id == filters.rule_id)
    if filters.file_path:
        conditions.append(FindingRecord.file_path.startswith(filters.file_path, autoescape=True))
    if filters.provenance:
        conditions.append(FindingRecord.provenance == filters.provenance.value)
    if filters.min_priority is not None:
        conditions.append(FindingRecord.priority_score >= filters.min_priority)
    total = session.scalar(select(func.count()).select_from(FindingRecord).where(*conditions))
    stmt = (
        select(FindingRecord)
        .where(*conditions)
        .order_by(
            FindingRecord.priority_score.desc(),
            FindingRecord.file_path,
            FindingRecord.line_start,
            FindingRecord.rule_id,
        )
        .limit(limit)
        .offset(offset)
    )
    return int(total or 0), [Finding.model_validate(r.data) for r in session.scalars(stmt)]


def get_finding(session: Session, audit_id: str, finding_id: str) -> Finding:
    get_audit(session, audit_id)
    record = session.get(FindingRecord, finding_record_id(audit_id, finding_id))
    if record is None or record.audit_id != audit_id:
        raise NotFoundError(f"finding {finding_id} not found in audit {audit_id}")
    return Finding.model_validate(record.data)


def require_code_index(audit: AuditRun) -> CodeIndex:
    if audit.code_index is None:
        raise ConflictError(
            f"audit {audit.id} has no code index yet (status {audit.status}, stage {audit.stage})",
            details={"status": audit.status, "stage": audit.stage},
        )
    return CodeIndex.model_validate(audit.code_index)


def file_finding_counts(session: Session, audit_id: str) -> dict[str, dict[str, Any]]:
    get_audit(session, audit_id)
    stmt = (
        select(
            FindingRecord.file_path,
            FindingRecord.severity,
            func.count(),
            func.max(FindingRecord.priority_score),
        )
        .where(FindingRecord.audit_id == audit_id)
        .group_by(FindingRecord.file_path, FindingRecord.severity)
    )
    counts: dict[str, dict[str, Any]] = {}
    for file_path, severity, count, max_priority in session.execute(stmt):
        entry = counts.setdefault(file_path, {"total": 0, "by_severity": {}, "max_priority": 0.0})
        entry["total"] += count
        entry["by_severity"][severity] = count
        entry["max_priority"] = max(entry["max_priority"], float(max_priority or 0.0))
    return dict(sorted(counts.items()))
