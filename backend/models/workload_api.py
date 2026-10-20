"""API/CLI response models for projects, workloads, profiles, and benchmarks.

The API routes and ``scripts/run_workload.py`` both build these from the same service functions,
so command-line and HTTP results are identical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.models.enums import Provenance


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    kind: str
    workload_count: int
    created_at: datetime


class WorkloadOut(BaseModel):
    id: str
    project_id: str
    slug: str
    name: str
    description: str
    entrypoint: str
    fixture_id: str
    invocation_mode: str
    default_size: str
    sizes: list[str]
    has_reference_candidate: bool
    tree_sha256: str | None


class SourceFileOut(BaseModel):
    path: str
    content: str
    truncated: bool


class WorkloadDetailOut(WorkloadOut):
    spec: dict[str, Any]
    fixture_description: str
    size_parameters: dict[str, dict[str, Any]]
    suspected_bottleneck: str
    entrypoint_source: SourceFileOut
    fixture_source: SourceFileOut
    readme: str | None


class HotspotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rank: int
    function_name: str
    file_path: str
    location_kind: str
    line_number: int
    primitive_calls: int
    total_calls: int
    internal_time_ms: float
    cumulative_time_ms: float
    percentage_of_profiled_time: float
    cumulative_percentage: float
    source_available: bool
    is_user_code: bool


class ProfileRunOut(BaseModel):
    id: str
    workload_id: str
    job_id: str | None
    status: str
    provenance: Provenance
    size: str
    enable_sampling: bool
    input_sha256: str | None
    total_profiled_time_ms: float | None
    wall_time_ms: float | None
    metrics: dict[str, Any]
    memory: dict[str, Any]
    sampling: dict[str, Any]
    environment: dict[str, Any]
    warnings: list[Any]
    top_user_hotspots: list[HotspotOut]
    artifacts: dict[str, str | None]
    source_snapshot: dict[str, Any] | None
    error: str | None
    error_details: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BenchmarkRunOut(BaseModel):
    id: str
    workload_id: str
    job_id: str | None
    status: str
    provenance: Provenance
    size: str
    settings: dict[str, Any]
    candidate: dict[str, Any]
    baseline_snapshot: dict[str, Any] | None
    candidate_snapshot: dict[str, Any] | None
    input_sha256: str | None
    classification: str | None
    classification_reason: str | None
    speedup: float | None
    speedup_ci_low: float | None
    speedup_ci_high: float | None
    latency_reduction_percent: float | None
    summary: dict[str, Any]
    memory: dict[str, Any]
    correctness: dict[str, Any]
    environment: dict[str, Any]
    warnings: list[Any]
    results_artifact_id: str | None
    error: str | None
    error_details: dict[str, Any] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BenchmarkSampleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    variant: str
    kind: str
    pair_index: int
    position_in_pair: int
    wall_ns: int | None
    cpu_ns: int | None
    tracemalloc_peak_bytes: int | None
    rss_peak_bytes: int | None
    ru_maxrss_bytes: int | None
    input_sha256: str | None
    output_sha256: str | None
    ok: bool
    error: str | None
