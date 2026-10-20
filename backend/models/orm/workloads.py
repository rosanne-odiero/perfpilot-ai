"""Workload deep-dive tables: projects, workloads, source snapshots, profiles, and benchmarks.

Binary artifacts (``.prof`` files, raw result dumps) live in the artifact store; rows only hold
artifact ids. Analysis/recommendation/candidate/validation tables are owned by later phases;
``BenchmarkRun.candidate_id`` is a plain string so it can point at them without a schema change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.database import Base, TimestampMixin, new_id


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(32), default="bundled_examples")
    root_path: Mapped[str] = mapped_column(String(1024))


class Workload(TimestampMixin, Base):
    __tablename__ = "workloads"
    __table_args__ = (UniqueConstraint("project_id", "slug"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    slug: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    workspace_path: Mapped[str] = mapped_column(String(1024))
    manifest: Mapped[dict[str, Any]] = mapped_column(
        doc="backend.models.workloads.WorkloadManifest"
    )
    source_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tree_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SourceSnapshot(TimestampMixin, Base):
    """Content hashes of a source tree at the moment it was profiled or benchmarked."""

    __tablename__ = "source_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    role: Mapped[str] = mapped_column(String(32), doc="baseline | reference | candidate")
    label: Mapped[str] = mapped_column(String(255), default="")
    root_path: Mapped[str] = mapped_column(String(1024))
    tree_sha256: Mapped[str] = mapped_column(String(64), index=True)
    files: Mapped[dict[str, Any]] = mapped_column(default=dict, doc="relative path -> sha256")


class ProfileRun(TimestampMixin, Base):
    __tablename__ = "profile_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_snapshots.id"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    size: Mapped[str] = mapped_column(String(32))
    enable_sampling: Mapped[bool] = mapped_column(default=False)
    provenance: Mapped[str] = mapped_column(String(16), default="MEASURED")
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_profiled_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    wall_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    memory: Mapped[dict[str, Any]] = mapped_column(default=dict)
    sampling: Mapped[dict[str, Any]] = mapped_column(default=dict)
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    prof_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    json_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"), nullable=True)
    sampling_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Hotspot(TimestampMixin, Base):
    __tablename__ = "hotspots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_run_id: Mapped[str] = mapped_column(ForeignKey("profile_runs.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, doc="1 = largest exclusive time")
    function_name: Mapped[str] = mapped_column(String(512))
    file_path: Mapped[str] = mapped_column(String(1024))
    location_kind: Mapped[str] = mapped_column(String(32), index=True)
    line_number: Mapped[int] = mapped_column(Integer)
    primitive_calls: Mapped[int] = mapped_column(BigInteger)
    total_calls: Mapped[int] = mapped_column(BigInteger)
    internal_time_ms: Mapped[float] = mapped_column(Float)
    cumulative_time_ms: Mapped[float] = mapped_column(Float)
    percentage_of_profiled_time: Mapped[float] = mapped_column(Float)
    cumulative_percentage: Mapped[float] = mapped_column(Float)
    source_available: Mapped[bool] = mapped_column(default=False)
    is_user_code: Mapped[bool] = mapped_column(default=False, index=True)


class BenchmarkRun(TimestampMixin, Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workload_id: Mapped[str] = mapped_column(ForeignKey("workloads.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    provenance: Mapped[str] = mapped_column(String(16), default="MEASURED")
    size: Mapped[str] = mapped_column(String(32))
    warmups: Mapped[int] = mapped_column(Integer)
    runs: Mapped[int] = mapped_column(Integer)
    memory_runs: Mapped[int] = mapped_column(Integer)
    threshold_percent: Mapped[float] = mapped_column(Float)
    seed: Mapped[int] = mapped_column(BigInteger)
    order: Mapped[str] = mapped_column(String(16))
    bootstrap_resamples: Mapped[int] = mapped_column(Integer)
    candidate_kind: Mapped[str] = mapped_column(String(16), doc="reference | candidate")
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    candidate_label: Mapped[str] = mapped_column(String(255))
    candidate_source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baseline_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_snapshots.id"), nullable=True
    )
    candidate_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_snapshots.id"), nullable=True
    )
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    speedup: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    speedup_ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_reduction_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    memory: Mapped[dict[str, Any]] = mapped_column(default=dict)
    correctness: Mapped[dict[str, Any]] = mapped_column(default=dict)
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    results_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BenchmarkSample(TimestampMixin, Base):
    """One measured process run of one variant (timing or memory)."""

    __tablename__ = "benchmark_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    benchmark_run_id: Mapped[str] = mapped_column(ForeignKey("benchmark_runs.id"), index=True)
    variant: Mapped[str] = mapped_column(String(16), doc="baseline | candidate")
    kind: Mapped[str] = mapped_column(String(16), doc="timing | memory")
    pair_index: Mapped[int] = mapped_column(Integer)
    position_in_pair: Mapped[int] = mapped_column(Integer, doc="0 = ran first in its pair")
    wall_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpu_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tracemalloc_peak_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rss_peak_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ru_maxrss_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ok: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
