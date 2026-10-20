"""Optimization candidates for imported repositories (Runtime Audit deep dive).

A candidate patches one finding of an audit's snapshot. ``state`` holds
:class:`backend.optimization.state_machine.OptimizationState` values and only changes through that
state machine; every change is appended to ``state_history``. Validation and benchmark runs execute
in the sandbox; their raw outputs are stored here (benchmark samples one row per container run).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base, TimestampMixin, new_id


class RepoOptimizationCandidate(TimestampMixin, Base):
    __tablename__ = "repo_optimization_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("repository_snapshots.id"), index=True)
    finding_id: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(String(32))
    finding_title: Mapped[str] = mapped_column(String(255), default="")
    evidence: Mapped[str] = mapped_column(
        String(16), doc="MEASURED when the finding had profiler evidence, else STATIC"
    )
    measured_evidence: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="RECOMMENDED", index=True)
    state_history: Mapped[list[Any]] = mapped_column(default=list)
    generated_by: Mapped[str] = mapped_column(String(128), doc="'deterministic' or model id")
    provenance: Mapped[str] = mapped_column(
        String(16), doc="STATIC or AI_HYPOTHESIS; MEASURED once benchmarked"
    )
    transform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_file: Mapped[str] = mapped_column(String(1024))
    target_function: Mapped[str | None] = mapped_column(String(512), nullable=True)
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    original_file_sha256: Mapped[str] = mapped_column(String(64))
    original_tree_sha256: Mapped[str] = mapped_column(String(64))
    proposed_diff: Mapped[str] = mapped_column(Text, default="")
    patched_file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patched_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_source: Mapped[str] = mapped_column(Text, default="")
    patched_source: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[list[Any]] = mapped_column(default=list)
    proposer_attempts: Mapped[list[Any]] = mapped_column(
        default=list, doc="Every proposer consulted and why it did (not) produce a patch"
    )
    llm_usage: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    patch_validation: Mapped[dict[str, Any]] = mapped_column(default=dict)
    approval_status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejection_reasons: Mapped[list[Any]] = mapped_column(default=list)
    workspace_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(16), default="NOT_RUN")
    validation_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    benchmark_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RepoValidationRun(TimestampMixin, Base):
    """The repository's approved test command run on the original snapshot and the patched copy."""

    __tablename__ = "repo_validation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("repo_optimization_candidates.id"), index=True
    )
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("runtime_approvals.id"))
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="NOT_RUN", index=True)
    argv: Mapped[list[Any]] = mapped_column(default=list)
    workdir_rel: Mapped[str] = mapped_column(String(1024), default=".")
    image: Mapped[str] = mapped_column(String(512), default="")
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    checks: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    variants: Mapped[dict[str, Any]] = mapped_column(
        default=dict, doc="Per variant: exit codes, outcome counts, durations, artifact ids"
    )
    test_comparison: Mapped[dict[str, Any]] = mapped_column(default=dict)
    patched_function_executed: Mapped[bool | None] = mapped_column(nullable=True)
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    original_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patched_tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    logs_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepoBenchmarkRun(TimestampMixin, Base):
    """Paired A/B benchmark of the approved workload command: original snapshot vs. patched copy."""

    __tablename__ = "repo_benchmark_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("repo_optimization_candidates.id"), index=True
    )
    audit_id: Mapped[str] = mapped_column(ForeignKey("audit_runs.id"), index=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("runtime_approvals.id"))
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)
    argv: Mapped[list[Any]] = mapped_column(default=list)
    workdir_rel: Mapped[str] = mapped_column(String(1024), default=".")
    image: Mapped[str] = mapped_column(String(512), default="")
    limits: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warmups: Mapped[int] = mapped_column(Integer)
    runs: Mapped[int] = mapped_column(Integer)
    threshold_percent: Mapped[float] = mapped_column(Float)
    seed: Mapped[int] = mapped_column(Integer)
    bootstrap_resamples: Mapped[int] = mapped_column(Integer, default=10_000)
    order: Mapped[list[Any]] = mapped_column(default=list, doc="Executed (phase, pair, variant)")
    inputs: Mapped[dict[str, Any]] = mapped_column(
        default=dict, doc="Per variant: argv, request hash, workspace tree hash; differing files"
    )
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    speedup: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RepoBenchmarkSample(TimestampMixin, Base):
    """One container run of one variant (warmups are stored too, flagged ``warmup``)."""

    __tablename__ = "repo_benchmark_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    benchmark_id: Mapped[str] = mapped_column(ForeignKey("repo_benchmark_runs.id"), index=True)
    variant: Mapped[str] = mapped_column(String(16))
    warmup: Mapped[bool] = mapped_column(default=False)
    pair_index: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer, doc="Execution order within the benchmark")
    wall_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpu_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    peak_rss_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    command_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    container_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    container_duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    ok: Mapped[bool] = mapped_column(default=False)
    failure: Mapped[str | None] = mapped_column(Text, nullable=True)
