"""Runtime audit results: sandbox profile runs, their hotspots, hotspot/finding correlations, and
the sandbox availability last reported by a worker (the API never talks to Docker itself)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base, TimestampMixin, new_id, utcnow


class RuntimeRunStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING_DEPENDENCIES = "PREPARING_DEPENDENCIES"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    OOM_KILLED = "OOM_KILLED"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def is_active(self) -> bool:
        return self in {
            RuntimeRunStatus.QUEUED,
            RuntimeRunStatus.PREPARING_DEPENDENCIES,
            RuntimeRunStatus.RUNNING,
        }


class RuntimeProfileRun(TimestampMixin, Base):
    """One approved command executed once in the sandbox under the profiling harness."""

    __tablename__ = "runtime_profile_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("repository_snapshots.id"), index=True)
    command_id: Mapped[str] = mapped_column(ForeignKey("runtime_commands.id"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("runtime_approvals.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=RuntimeRunStatus.QUEUED, index=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    argv: Mapped[list[Any]] = mapped_column(doc="Exact approved argv")
    workdir_rel: Mapped[str] = mapped_column(String(1024))
    command_kind: Mapped[str] = mapped_column(String(32))
    approved_by: Mapped[str] = mapped_column(String(255))
    trace_memory: Mapped[bool] = mapped_column(default=True)
    allow_dependency_install: Mapped[bool] = mapped_column(default=False)
    dependency_install: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(default=False)
    oom_killed: Mapped[bool] = mapped_column(default=False)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpu_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_profiled_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    function_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    peak_rss_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rss_at_exit_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tracemalloc_peak_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tracemalloc_retained_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    stdout_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    prof_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    stats_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuntimeHotspotRecord(TimestampMixin, Base):
    """A profiled function of a successful run (MEASURED). Failed runs store none."""

    __tablename__ = "runtime_hotspots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_profile_runs.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    function_name: Mapped[str] = mapped_column(String(512))
    file_path: Mapped[str] = mapped_column(String(1024))
    line_number: Mapped[int] = mapped_column(Integer)
    location_kind: Mapped[str] = mapped_column(String(32))
    is_user_code: Mapped[bool] = mapped_column(default=False, index=True)
    primitive_calls: Mapped[int] = mapped_column(Integer)
    total_calls: Mapped[int] = mapped_column(Integer)
    internal_time_ms: Mapped[float] = mapped_column(Float)
    cumulative_time_ms: Mapped[float] = mapped_column(Float)
    attributed_time_ms: Mapped[float] = mapped_column(Float)
    percentage_of_profiled_time: Mapped[float] = mapped_column(Float)
    cumulative_percentage: Mapped[float] = mapped_column(Float)
    attributed_percentage: Mapped[float] = mapped_column(Float)
    symbol: Mapped[str | None] = mapped_column(String(512), nullable=True)


class RuntimeCorrelationRecord(TimestampMixin, Base):
    """A measured hotspot linked to a static finding, or a runtime-only hotspot."""

    __tablename__ = "runtime_hotspot_correlations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runtime_profile_runs.id"), index=True)
    hotspot_id: Mapped[str] = mapped_column(ForeignKey("runtime_hotspots.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), doc="'finding' or 'runtime_only'")
    finding_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(doc="backend.audit.correlation contracts")


class RuntimeBackendStatusRecord(TimestampMixin, Base):
    """Sandbox availability as last observed by a worker process."""

    __tablename__ = "runtime_backend_status"

    slot: Mapped[str] = mapped_column(String(32), primary_key=True, doc="Configured backend")
    backend: Mapped[str] = mapped_column(String(64), doc="Implementation that answered")
    available: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
