"""API contracts for the Runtime Audit (``/api/runtime`` and ``/api/audits/{id}/runtime``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.audit.correlation import FindingCorrelation, RuntimeOnlyHotspot
from backend.models.enums import AuditMode, AuditStage, AuditStatus, Provenance
from backend.sandbox.command_discovery import CommandKind, RiskLevel

MeasuredLabel = Literal[Provenance.MEASURED]


class LimitsOverride(BaseModel):
    """Optional per-run limits. Each value may only be tighter than the configured default."""

    model_config = ConfigDict(extra="forbid")

    cpus: float | None = Field(default=None, gt=0, le=64)
    memory_mb: int | None = Field(default=None, ge=32)
    pids: int | None = Field(default=None, ge=8)
    timeout_seconds: int | None = Field(default=None, ge=1, le=24 * 3600)
    tmpfs_mb: int | None = Field(default=None, ge=1)
    output_max_bytes: int | None = Field(default=None, ge=1024)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=36)
    argv: list[str] = Field(
        min_length=1,
        max_length=64,
        description="Must equal the discovered argv exactly; the approval pins it",
    )
    approved_by: str = Field(min_length=1, max_length=255)
    allow_dependency_install: bool = False
    allow_sdist_builds: bool = False
    limits_override: LimitsOverride | None = None
    trace_memory: bool = Field(
        default=True,
        description="Run tracemalloc concurrently with cProfile (adds overhead to timings)",
    )


class RuntimeLimitsOut(BaseModel):
    cpus: float
    memory_mb: int
    pids: int
    timeout_seconds: int
    tmpfs_mb: int
    output_max_bytes: int
    network: Literal["none"] = "none"


class RuntimeStatusOut(BaseModel):
    backend: str
    available: bool
    reason: str | None
    image: str
    default_limits: RuntimeLimitsOut
    dependency_install_timeout_seconds: int
    approval_ttl_seconds: int
    details: dict[str, Any]
    checked_at: datetime | None
    observed_by: Literal["worker"] = "worker"


class RuntimeCommandOut(BaseModel):
    id: str
    command_key: str
    kind: CommandKind
    argv: list[str]
    workdir_rel: str
    description: str
    evidence: list[str]
    requires_dependencies: bool
    third_party_imports: list[str]
    estimated_risk: RiskLevel
    runnable: bool
    reason_if_not_runnable: str | None
    provenance: Provenance
    discovered_at: datetime


class HotspotOut(BaseModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    id: str
    rank: int
    function_name: str
    symbol: str | None
    file_path: str
    line_number: int
    location_kind: str
    is_user_code: bool
    primitive_calls: int
    total_calls: int
    internal_time_ms: float
    cumulative_time_ms: float
    attributed_time_ms: float
    percentage_of_profiled_time: float
    cumulative_percentage: float
    attributed_percentage: float


class MemoryOut(BaseModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    peak_rss_bytes: int | None = Field(
        description="getrusage ru_maxrss of the profiled process inside the sandbox"
    )
    rss_at_exit_bytes: int | None
    tracemalloc_peak_bytes: int | None = Field(
        description="Peak of Python-allocator allocations (tracemalloc) during the command"
    )
    tracemalloc_retained_bytes: int | None


class RunSummaryOut(BaseModel):
    id: str
    status: str
    failure_reason: str | None
    argv: list[str]
    workdir_rel: str
    command_kind: str
    command_id: str
    approval_id: str
    approved_by: str
    job_id: str | None
    exit_code: int | None
    command_exit_code: int | None
    timed_out: bool
    oom_killed: bool
    duration_s: float | None
    wall_time_ms: float | None
    cpu_time_ms: float | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunDetailOut(RunSummaryOut):
    provenance: MeasuredLabel = Provenance.MEASURED
    limits: dict[str, Any]
    trace_memory: bool
    allow_dependency_install: bool
    dependency_install: dict[str, Any] | None
    total_profiled_time_ms: float | None
    function_count: int | None
    memory: MemoryOut
    environment: dict[str, Any]
    stdout_tail: str | None
    stderr_tail: str | None
    artifacts: dict[str, str | None]
    warnings: list[Any]
    hotspots: list[HotspotOut]
    correlations: list[FindingCorrelation]
    runtime_only_hotspots: list[RuntimeOnlyHotspot]


class RuntimeAuditOut(BaseModel):
    audit_id: str
    mode: AuditMode
    audit_status: AuditStatus
    stage: AuditStage
    runtime_requested: bool
    runtime_available: bool
    unavailable_reason: str | None
    backend: str
    commands_discovered: int
    runs: list[RunSummaryOut]
    measured_run_id: str | None = Field(description="Run whose evidence is on the findings")
    hotspots: list[HotspotOut]
    correlations: list[FindingCorrelation]
    runtime_only_hotspots: list[RuntimeOnlyHotspot]
    warnings: list[Any]
