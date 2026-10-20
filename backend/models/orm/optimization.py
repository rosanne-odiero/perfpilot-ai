"""Deep-dive optimization tables: analyses, recommendations, candidates and validation runs.

State columns hold :class:`backend.optimization.state_machine.OptimizationState` values and are
only changed through that state machine; every change is appended to ``state_history``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base, TimestampMixin, new_id


class AnalysisRun(TimestampMixin, Base):
    """Static rules over a workload's source, correlated with a profile's measured hotspots."""

    __tablename__ = "analysis_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    profile_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("profile_runs.id"), nullable=True, index=True
    )
    profile_requested: Mapped[bool] = mapped_column(
        default=False, doc="True when the analysis ran its own profile"
    )
    profile_size: Mapped[str | None] = mapped_column(String(32), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    state_history: Mapped[list[Any]] = mapped_column(default=list)
    source_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_snapshots.id"), nullable=True
    )
    findings: Mapped[list[Any]] = mapped_column(default=list, doc="STATIC findings (contracts)")
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OptimizationRecommendation(TimestampMixin, Base):
    """Base-spec recommendation. Numbers appear only inside ``profiler_evidence`` (MEASURED);
    ``estimated_impact`` is qualitative."""

    __tablename__ = "optimization_recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    analysis_run_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), index=True)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    rule_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    finding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    target_file: Mapped[str] = mapped_column(String(1024))
    target_file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_function: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_lines: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="{start, end}")
    observed_bottleneck: Mapped[str] = mapped_column(Text)
    profiler_evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    suspected_root_cause: Mapped[str] = mapped_column(Text)
    proposed_optimization: Mapped[str] = mapped_column(Text)
    expected_effect: Mapped[str] = mapped_column(Text)
    correctness_risks: Mapped[list[Any]] = mapped_column(default=list)
    memory_tradeoffs: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[str] = mapped_column(String(16))
    validation_plan: Mapped[list[Any]] = mapped_column(default=list)
    estimated_impact: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(
        default=dict, doc="Provenance label per part of the recommendation"
    )
    generated_by: Mapped[str] = mapped_column(String(128), default="deterministic")
    patch_strategy: Mapped[dict[str, Any]] = mapped_column(
        default=dict, doc="Dry-run result: is a deterministic patch available, and why (not)"
    )


class OptimizationCandidate(TimestampMixin, Base):
    __tablename__ = "optimization_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("optimization_recommendations.id"), index=True
    )
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="RECOMMENDED", index=True)
    state_history: Mapped[list[Any]] = mapped_column(default=list)
    generated_by: Mapped[str] = mapped_column(String(128), doc="'deterministic' or model id")
    transform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_file: Mapped[str] = mapped_column(String(1024))
    original_file_sha256: Mapped[str] = mapped_column(String(64))
    original_tree_sha256: Mapped[str] = mapped_column(String(64))
    proposed_diff: Mapped[str] = mapped_column(Text, default="")
    patched_file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patched_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_source: Mapped[str] = mapped_column(Text, default="")
    patched_source: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[list[Any]] = mapped_column(default=list)
    patch_validation: Mapped[dict[str, Any]] = mapped_column(default=dict)
    approval_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejection_reasons: Mapped[list[Any]] = mapped_column(default=list)
    patched_workspace_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(16), default="NOT_RUN")
    validation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    benchmark_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("benchmark_runs.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ValidationRun(TimestampMixin, Base):
    __tablename__ = "validation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("optimization_candidates.id"), index=True)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="NOT_RUN", index=True)
    checks: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    baseline_input_hashes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    original_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patched_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    logs_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
