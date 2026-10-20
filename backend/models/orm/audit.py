"""Repository Audit tables. Rich payloads are stored as JSON of the contracts in
``backend.models.audit_schemas``; frequently-filtered fields are promoted to columns."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base, TimestampMixin, new_id
from backend.models.enums import AuditStage, AuditStatus, SnapshotStatus


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    source_type: Mapped[str] = mapped_column(String(32))
    source_locator: Mapped[str] = mapped_column(String(1024), doc="URL/path, credentials stripped")
    requested_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)


class RepositorySnapshot(TimestampMixin, Base):
    __tablename__ = "repository_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    repository_id: Mapped[str] = mapped_column(ForeignKey("repositories.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=SnapshotStatus.PENDING)
    workspace_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    git: Mapped[dict[str, Any]] = mapped_column(default=dict)
    inventory: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class AuditRun(TimestampMixin, Base):
    __tablename__ = "audit_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("repository_snapshots.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=AuditStatus.PENDING)
    stage: Mapped[str] = mapped_column(String(32), default=AuditStage.QUEUED)
    use_llm: Mapped[bool] = mapped_column(default=False)
    llm_consent: Mapped[bool] = mapped_column(default=False)
    code_index: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    insights: Mapped[list[Any]] = mapped_column(default=list)
    llm_usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class FindingRecord(TimestampMixin, Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(32), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[str] = mapped_column(String(16))
    provenance: Mapped[str] = mapped_column(String(16), index=True)
    file_path: Mapped[str] = mapped_column(String(1024), index=True)
    line_start: Mapped[int] = mapped_column(Integer)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(doc="backend.models.audit_schemas.Finding")


class RecommendationRecord(TimestampMixin, Base):
    __tablename__ = "audit_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    priority_rank: Mapped[int] = mapped_column(Integer, default=0)
    provenance: Mapped[str] = mapped_column(String(16))
    generated_by: Mapped[str] = mapped_column(String(128))
    data: Mapped[dict[str, Any]] = mapped_column(doc="backend.models.audit_schemas.Recommendation")


class ReportRecord(TimestampMixin, Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    subject_type: Mapped[str] = mapped_column(String(32), default="audit")
    subject_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    format: Mapped[str] = mapped_column(String(8))
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
