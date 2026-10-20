"""Explicit, single-use approvals for running discovered commands in the sandbox.

Rules enforced here (and covered by tests):

* only commands that discovery recorded for the audit can be approved, and the argv the user
  approves must equal the recorded argv exactly, so a client cannot inject arguments;
* the image is always the configured sandbox image, and limits may only be *tighter* than the
  configured defaults;
* approvals expire, can be revoked, and are consumed exactly once for execution;
* dependency installation (the only network-enabled step) needs its own flag on the approval and
  is consumed separately, before execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.exceptions import (
    ConflictError,
    NotFoundError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.models.database import utcnow
from backend.models.orm.audit import AuditRun, RepositorySnapshot
from backend.models.orm.runtime import RuntimeApproval, RuntimeApprovalStatus, RuntimeCommand
from backend.sandbox.backend import SandboxLimits
from backend.sandbox.command_discovery import CandidateCommand, validate_command_argv


class ApprovedExecution(BaseModel):
    """What the worker may run after consuming an approval. Nothing else."""

    model_config = ConfigDict(frozen=True)

    approval_id: str
    audit_id: str
    snapshot_id: str
    command_id: str
    argv: list[str]
    workdir_rel: str
    image: str
    limits: SandboxLimits


class DependencyInstallGrant(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    snapshot_id: str
    image: str
    allow_sdist_builds: bool


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; everything is stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def record_discovered_commands(
    session: Session, audit_id: str, snapshot_id: str, commands: Sequence[CandidateCommand]
) -> list[RuntimeCommand]:
    """Persist discovery results for an audit. Idempotent per stable command id."""
    audit = session.get(AuditRun, audit_id)
    if audit is None:
        raise NotFoundError(f"audit {audit_id} not found")
    if audit.snapshot_id != snapshot_id:
        raise ValidationFailedError("snapshot does not belong to this audit")
    existing = {
        row.command_key: row
        for row in session.scalars(
            select(RuntimeCommand).where(RuntimeCommand.audit_id == audit_id)
        )
    }
    records: list[RuntimeCommand] = []
    for command in commands:
        record = existing.get(command.id)
        if record is None:
            record = RuntimeCommand(
                audit_id=audit_id,
                snapshot_id=snapshot_id,
                command_key=command.id,
                kind=command.kind,
                runnable=command.runnable,
                command=command.model_dump(mode="json"),
            )
            session.add(record)
            existing[command.id] = record
        records.append(record)
    session.flush()
    return records


def _snapshot_workspace(session: Session, snapshot_id: str) -> Path:
    snapshot = session.get(RepositorySnapshot, snapshot_id)
    if snapshot is None or not snapshot.workspace_path:
        raise ConflictError("snapshot has no workspace to run commands in")
    workspace = Path(snapshot.workspace_path)
    if not workspace.is_dir():
        raise ConflictError("snapshot workspace no longer exists")
    return workspace


def _clean_label(value: str) -> str:
    label = value.strip()
    if not label or len(label) > 255 or any(not ch.isprintable() for ch in label):
        raise ValidationFailedError("approved_by must be 1-255 printable characters")
    return label


def approve_command(
    session: Session,
    audit_id: str,
    command_id: str,
    *,
    argv: list[str],
    approved_by: str,
    allow_dependency_install: bool = False,
    allow_sdist_builds: bool = False,
    limits_override: SandboxLimits | None = None,
    settings: Settings | None = None,
) -> RuntimeApproval:
    """Approve one recorded command, pinning its exact argv, image and limits."""
    settings = settings or get_settings()
    record = session.get(RuntimeCommand, command_id)
    if record is None or record.audit_id != audit_id:
        raise NotFoundError(f"command {command_id} not found for audit {audit_id}")
    command = CandidateCommand.model_validate(record.command)
    if not command.runnable:
        raise ConflictError(
            f"command is not runnable: {command.reason_if_not_runnable or 'information only'}"
        )
    if not isinstance(argv, list) or argv != command.argv:
        raise SecurityViolationError(
            "approved argv must exactly match the discovered command",
            details={"expected": command.argv},
        )
    validate_command_argv(
        command.argv,
        workspace_dir=_snapshot_workspace(session, record.snapshot_id),
        workdir_rel=command.workdir_rel,
    )
    if allow_sdist_builds and not allow_dependency_install:
        raise ValidationFailedError("allow_sdist_builds requires allow_dependency_install")
    defaults = SandboxLimits.from_settings(settings)
    limits = limits_override or defaults
    looser = limits.exceeding(defaults)
    if looser:
        raise ValidationFailedError(
            "limit overrides may only tighten the configured defaults",
            details={"looser_than_default": looser},
        )
    now = utcnow()
    approval = RuntimeApproval(
        audit_id=audit_id,
        command_id=record.id,
        snapshot_id=record.snapshot_id,
        argv=list(command.argv),
        workdir_rel=command.workdir_rel,
        image=settings.sandbox_image,
        limits=limits.model_dump(),
        allow_dependency_install=allow_dependency_install,
        allow_sdist_builds=allow_sdist_builds,
        approved_by=_clean_label(approved_by),
        approved_at=now,
        status=RuntimeApprovalStatus.APPROVED,
        expires_at=now + timedelta(seconds=settings.sandbox_approval_ttl_seconds),
    )
    session.add(approval)
    session.flush()
    return approval


def revoke_approval(session: Session, approval_id: str, *, reason: str | None = None) -> None:
    result = session.execute(
        update(RuntimeApproval)
        .where(
            RuntimeApproval.id == approval_id,
            RuntimeApproval.status.in_(
                [RuntimeApprovalStatus.PENDING, RuntimeApprovalStatus.APPROVED]
            ),
        )
        .values(status=RuntimeApprovalStatus.REVOKED, revoked_at=utcnow(), revoke_reason=reason)
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        _load_usable(session, approval_id)  # raises the precise reason
        raise ConflictError("approval could not be revoked")


def _load_usable(session: Session, approval_id: str) -> RuntimeApproval:
    approval = session.get(RuntimeApproval, approval_id)
    if approval is None:
        raise NotFoundError(f"approval {approval_id} not found")
    if approval.status == RuntimeApprovalStatus.CONSUMED:
        raise ConflictError("approval has already been used")
    if approval.status == RuntimeApprovalStatus.REVOKED:
        raise ConflictError("approval has been revoked")
    if approval.status != RuntimeApprovalStatus.APPROVED:
        raise ConflictError(f"approval is {approval.status}, not APPROVED")
    if _aware(approval.expires_at) <= utcnow():
        raise ConflictError("approval has expired")
    return approval


def consume_approval(session: Session, approval_id: str) -> ApprovedExecution:
    """Atomically mark an approval CONSUMED and return exactly what may be executed."""
    approval = _load_usable(session, approval_id)
    result = session.execute(
        update(RuntimeApproval)
        .where(
            RuntimeApproval.id == approval_id,
            RuntimeApproval.status == RuntimeApprovalStatus.APPROVED,
        )
        .values(status=RuntimeApprovalStatus.CONSUMED, consumed_at=utcnow())
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ConflictError("approval has already been used")
    return ApprovedExecution(
        approval_id=approval.id,
        audit_id=approval.audit_id,
        snapshot_id=approval.snapshot_id,
        command_id=approval.command_id,
        argv=[str(a) for a in approval.argv],
        workdir_rel=approval.workdir_rel,
        image=approval.image,
        limits=SandboxLimits.model_validate(approval.limits),
    )


def consume_dependency_install(session: Session, approval_id: str) -> DependencyInstallGrant:
    """Single-use grant for the network-enabled dependency install of an approved command."""
    approval = _load_usable(session, approval_id)
    if not approval.allow_dependency_install:
        raise ConflictError("dependency installation was not approved for this command")
    result = session.execute(
        update(RuntimeApproval)
        .where(
            RuntimeApproval.id == approval_id,
            RuntimeApproval.status == RuntimeApprovalStatus.APPROVED,
            RuntimeApproval.dependency_install_consumed_at.is_(None),
        )
        .values(dependency_install_consumed_at=utcnow())
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ConflictError("dependency installation has already been performed")
    return DependencyInstallGrant(
        approval_id=approval.id,
        snapshot_id=approval.snapshot_id,
        image=approval.image,
        allow_sdist_builds=approval.allow_sdist_builds,
    )
