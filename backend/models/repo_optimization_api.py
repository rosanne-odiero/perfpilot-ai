"""Request/response models for repository optimization candidates (``/api/repo-optimizations``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_ACTOR = r"^[A-Za-z0-9_.@ -]{1,128}$"
_ID = r"^[A-Za-z0-9_-]{1,64}$"

CandidateOutcome = Literal["proposed", "no_safe_patch", "rejected_by_validator", "rejected"]

DEFAULT_WARMUPS = 1
DEFAULT_RUNS = 10
DEFAULT_THRESHOLD_PERCENT = 5.0


class RepoOptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(pattern=_ID, description="A finding of this audit (stable finding id)")


class RepoApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(default="local-user", pattern=_ACTOR)


class RepoRejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    rejected_by: str = Field(default="local-user", pattern=_ACTOR)


class RepoValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str | None = Field(
        default=None,
        pattern=_ID,
        description="A specific approved pytest command of this audit (default: the latest)",
    )


class RepoBenchmarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warmups: int = Field(default=DEFAULT_WARMUPS, ge=0, le=10)
    runs: int = Field(default=DEFAULT_RUNS, ge=2, le=50, description="Measured runs per variant")
    threshold_percent: float = Field(default=DEFAULT_THRESHOLD_PERCENT, gt=0, lt=100)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    approval_id: str | None = Field(
        default=None,
        pattern=_ID,
        description="A specific approved workload command of this audit (default: the latest)",
    )


class RepoValidationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    audit_id: str
    approval_id: str
    job_id: str | None
    status: str
    provenance: Literal["MEASURED"] = "MEASURED"
    argv: list[str]
    workdir_rel: str
    image: str
    limits: dict[str, Any]
    checks: list[dict[str, Any]]
    summary: str
    variants: dict[str, Any]
    test_comparison: dict[str, Any]
    patched_function_executed: bool | None
    environment: dict[str, Any]
    original_tree_sha256: str | None
    patched_tree_sha256: str | None
    logs_artifact_id: str | None
    duration_seconds: float | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RepoBenchmarkSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    variant: str
    warmup: bool
    pair_index: int
    sequence: int
    wall_ns: int | None
    cpu_ns: int | None
    peak_rss_bytes: int | None
    command_exit_code: int | None
    container_exit_code: int | None
    container_duration_s: float | None
    ok: bool
    failure: str | None


class RepoBenchmarkOut(BaseModel):
    id: str
    candidate_id: str
    audit_id: str
    approval_id: str
    job_id: str | None
    status: str
    provenance: Literal["MEASURED"] = "MEASURED"
    argv: list[str]
    workdir_rel: str
    image: str
    limits: dict[str, Any]
    warmups: int
    runs: int
    threshold_percent: float
    seed: int
    timed: str
    inputs: dict[str, Any]
    summary: dict[str, Any]
    speedup: float | None
    speedup_ci: list[float | None]
    latency_reduction_percent: float | None
    classification: str | None
    classification_reason: str | None
    environment: dict[str, Any]
    warnings: list[Any]
    error: str | None
    samples: list[RepoBenchmarkSampleOut]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RepoCandidateOut(BaseModel):
    id: str
    audit_id: str
    snapshot_id: str
    finding_id: str
    rule_id: str
    finding_title: str
    evidence: Literal["MEASURED", "STATIC"] = Field(
        description="Whether the finding carried profiler evidence when the candidate was made"
    )
    measured_evidence: dict[str, Any] | None
    outcome: CandidateOutcome
    state: str
    state_history: list[dict[str, Any]]
    generated_by: str
    provenance: str
    transform: str | None
    target_file: str
    target_function: str | None
    line_start: int
    line_end: int
    original_file_sha256: str
    original_tree_sha256: str
    patched_file_sha256: str | None
    patched_tree_sha256: str | None
    summary: str
    notes: list[Any]
    proposer_attempts: list[Any]
    llm_usage: dict[str, Any] | None
    patch_validation: dict[str, Any]
    approval: dict[str, Any]
    rejection_reasons: list[Any]
    workspace_path: str | None
    validation_status: str
    validation: RepoValidationOut | None
    benchmark: RepoBenchmarkOut | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class PatchPreviewItem(BaseModel):
    """Whether a finding can get a patch right now, computed without creating anything."""

    finding_id: str
    rule_id: str
    title: str
    severity: str
    priority_score: float
    file_path: str
    line_start: int
    symbol: str | None
    measured: bool = Field(description="The finding has MEASURED runtime evidence")
    automatic_patch: bool = Field(description="A deterministic AST rewrite applies safely")
    transform: str | None = None
    reason: str | None = Field(default=None, description="Why no automatic patch is possible")
    llm_fallback: bool = Field(
        description="An LLM would be asked when no automatic patch applies (configured + consent)"
    )
    candidate_ids: list[str] = Field(default_factory=list)


class PatchPreviewOut(BaseModel):
    audit_id: str
    total_findings: int
    previewed: int
    automatic_patches: int
    llm_configured: bool
    llm_consent: bool
    patchable_rules: list[str]
    items: list[PatchPreviewItem]
