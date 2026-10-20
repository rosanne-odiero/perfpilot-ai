"""Request and response models for analyses, recommendations, candidates and validations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_ACTOR = r"^[A-Za-z0-9_.@ -]{1,128}$"


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload_id: str = Field(min_length=1, max_length=64)
    profile_id: str | None = Field(
        default=None,
        max_length=64,
        description="A SUCCEEDED profile of this workload; omitted = profile as part of the job",
    )
    size: str | None = Field(default=None, description="Profile size when profile_id is omitted")


class OptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_id: str = Field(min_length=1, max_length=64)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(default="local-user", pattern=_ACTOR)


class RejectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    rejected_by: str = Field(default="local-user", pattern=_ACTOR)


class AnalysisOut(BaseModel):
    id: str
    workload_id: str
    profile_run_id: str | None
    profile_requested: bool
    profile_size: str | None
    job_id: str | None
    state: str
    state_history: list[dict[str, Any]]
    source_snapshot: dict[str, Any] | None
    findings: list[dict[str, Any]]
    summary: dict[str, Any]
    warnings: list[Any]
    recommendation_ids: list[str]
    error: str | None
    error_details: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RecommendationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    analysis_run_id: str
    workload_id: str
    rank: int
    rule_id: str | None
    finding_id: str | None
    title: str
    target_file: str
    target_file_sha256: str | None
    target_function: str | None
    source_lines: dict[str, Any]
    observed_bottleneck: str
    profiler_evidence: dict[str, Any]
    suspected_root_cause: str
    proposed_optimization: str
    expected_effect: str
    correctness_risks: list[Any]
    memory_tradeoffs: str
    confidence: str
    validation_plan: list[Any]
    estimated_impact: dict[str, Any]
    provenance: dict[str, Any]
    generated_by: str
    patch_strategy: dict[str, Any]
    created_at: datetime


class ValidationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    candidate_id: str
    workload_id: str
    job_id: str | None
    status: str
    checks: list[Any]
    summary: str
    baseline_input_hashes: dict[str, Any]
    original_tree_sha256: str | None
    patched_tree_sha256: str | None
    logs_artifact_id: str | None
    duration_seconds: float | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CandidateOut(BaseModel):
    id: str
    recommendation_id: str
    workload_id: str
    state: str
    state_history: list[dict[str, Any]]
    generated_by: str
    transform: str | None
    target_file: str
    original_file_sha256: str
    original_tree_sha256: str
    patched_file_sha256: str | None
    patched_tree_sha256: str | None
    summary: str
    notes: list[Any]
    patch_validation: dict[str, Any]
    approval: dict[str, Any]
    rejection_reasons: list[Any]
    patched_workspace_path: str | None
    validation_status: str
    validation_run_id: str | None
    validation: ValidationRunOut | None
    benchmark_run_id: str | None
    benchmark: dict[str, Any] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class DiffFileOut(BaseModel):
    path: str
    original: str
    patched: str
    original_sha256: str
    patched_sha256: str | None


class DiffOut(BaseModel):
    candidate_id: str
    generated_by: str
    unified_diff: str
    files: list[DiffFileOut]
