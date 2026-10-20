"""Runtime audit tables: discovered commands and their explicit, single-use approvals."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base, TimestampMixin, new_id, utcnow


class RuntimeApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"
    CONSUMED = "CONSUMED"


class RuntimeCommand(TimestampMixin, Base):
    """A command proposed by static discovery. Never executed without a RuntimeApproval."""

    __tablename__ = "runtime_commands"
    __table_args__ = (UniqueConstraint("audit_id", "command_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("repository_snapshots.id"), index=True)
    command_key: Mapped[str] = mapped_column(String(32), doc="CandidateCommand.id (stable hash)")
    kind: Mapped[str] = mapped_column(String(32))
    runnable: Mapped[bool] = mapped_column(default=False)
    command: Mapped[dict[str, Any]] = mapped_column(
        doc="backend.sandbox.command_discovery.CandidateCommand"
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeApproval(TimestampMixin, Base):
    """Who approved exactly which argv, with which image and limits, until when."""

    __tablename__ = "runtime_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    command_id: Mapped[str] = mapped_column(ForeignKey("runtime_commands.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("repository_snapshots.id"), index=True)
    argv: Mapped[list[Any]] = mapped_column(doc="Exact argv snapshot at approval time")
    workdir_rel: Mapped[str] = mapped_column(String(1024))
    image: Mapped[str] = mapped_column(String(512))
    limits: Mapped[dict[str, Any]] = mapped_column(doc="backend.sandbox.backend.SandboxLimits")
    allow_dependency_install: Mapped[bool] = mapped_column(default=False)
    allow_sdist_builds: Mapped[bool] = mapped_column(default=False)
    approved_by: Mapped[str] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=RuntimeApprovalStatus.PENDING, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dependency_install_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
