"""The ``ExperimentReport`` contract: one workload optimization experiment, rendered to HTML,
Markdown, JSON, CSV and PDF (an engineering report to attach to a GitHub issue or pull request).

An experiment is either one ``OptimizationCandidate`` (profile -> recommendation -> patch ->
approval -> validation -> benchmark) or one benchmark of a workload's bundled reference
implementation. Sections that hold measurements pin ``provenance`` to ``MEASURED``; hypotheses
carry their own ``STATIC`` / ``AI_HYPOTHESIS`` label. A section that did not run is ``None`` and
the report states why (for example: no benchmark after a FAILED validation).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import Provenance

EXPERIMENT_SCHEMA_VERSION = "1.0"

MeasuredLabel = Literal[Provenance.MEASURED]

EXPERIMENT_SECTION_TITLES: dict[int, str] = {
    1: "Outcome: measured vs hypothesis",
    2: "Workload & input",
    3: "Environment fingerprint",
    4: "Profiling evidence",
    5: "Recommendation",
    6: "Proposed change (diff)",
    7: "Approval & lifecycle",
    8: "Correctness validation",
    9: "Benchmark results",
    10: "Reproducibility",
    11: "Limitations",
}

EXCLUSIVE_DEFINITION = (
    "Exclusive (pstats tottime): time spent in the function body itself, excluding callees. "
    "Exclusive percentages add up to 100% across all functions."
)
CUMULATIVE_DEFINITION = (
    "Cumulative (pstats cumtime): time in the function and everything it called. Cumulative "
    "percentages overlap (a caller includes its callees) and are never summed."
)
PROFILER_OVERHEAD_NOTE = (
    "cProfile instruments every call, so absolute profiled times are inflated; use the A/B "
    "benchmark for real timings."
)


class ExperimentKind(StrEnum):
    OPTIMIZATION = "optimization"
    REFERENCE_BENCHMARK = "reference_benchmark"


class ExperimentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExperimentMetadata(ExperimentModel):
    title: str
    subject_type: Literal["optimization", "benchmark"]
    subject_id: str
    generated_at: datetime
    generator: str
    pdf_engine: str | None = Field(default=None, description="Set only on PDF renders")
    redactions_applied: int = 0


class Outcome(ExperimentModel):
    """The one-paragraph answer: what happened to this experiment."""

    state: str = Field(description="Candidate state, or benchmark status for reference runs")
    headline: str
    succeeded: bool
    reasons: list[str] = Field(default_factory=list)


class WorkloadSection(ExperimentModel):
    workload_id: str
    slug: str
    name: str
    description: str
    project: str
    entrypoint: str
    invocation_mode: str
    fixture: str
    fixture_description: str
    size: str | None = Field(description="Fixture size of the benchmark (or profile)")
    size_parameters: dict[str, Any] = Field(default_factory=dict)
    input_sha256: str | None = None
    input_sha256_source: str | None = None
    expected_output: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int
    memory_limit_mb: int
    tree_sha256: str | None = Field(default=None, description="Registered source tree hash")


class EnvironmentSection(ExperimentModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    source: str = Field(description="Which run captured this fingerprint")
    fields: dict[str, str] = Field(default_factory=dict)
    packages: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class HotspotRow(ExperimentModel):
    rank: int
    function_name: str
    file_path: str
    line_number: int
    location_kind: str
    is_user_code: bool
    total_calls: int
    primitive_calls: int
    exclusive_ms: float
    cumulative_ms: float
    exclusive_percent: float
    cumulative_percent: float


class MemoryMetric(ExperimentModel):
    name: str
    tool: Literal["tracemalloc", "rss", "ru_maxrss"]
    value_bytes: float | None
    definition: str


class ProfilingSection(ExperimentModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    profile_id: str
    relation: str = Field(description="How this profile relates to the experiment")
    status: str
    size: str
    input_sha256: str | None
    total_profiled_time_ms: float | None
    wall_time_under_profiler_ms: float | None
    hotspots: list[HotspotRow]
    definitions: dict[str, str]
    memory: list[MemoryMetric]
    memory_unavailable: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LabeledText(ExperimentModel):
    text: str
    provenance: Provenance
    note: str | None = None


class RecommendationSection(ExperimentModel):
    recommendation_id: str
    analysis_id: str
    rank: int
    rule_id: str | None
    title: str
    target_file: str
    target_function: str | None
    source_lines: dict[str, Any]
    observed_bottleneck: LabeledText
    profiler_evidence: dict[str, Any]
    profiler_evidence_provenance: MeasuredLabel = Provenance.MEASURED
    suspected_root_cause: LabeledText
    proposed_optimization: LabeledText
    expected_effect: LabeledText
    estimated_impact: LabeledText
    correctness_risks: list[str]
    memory_tradeoffs: LabeledText
    confidence: str
    validation_plan: list[str]
    generated_by: str


class PatchSection(ExperimentModel):
    generated_by: str
    transform: str | None
    target_file: str
    summary: str
    notes: list[str]
    unified_diff: str
    diff_withheld_reason: str | None = None
    changed_lines: int | None
    original_file_sha256: str
    patched_file_sha256: str | None
    original_tree_sha256: str
    patched_tree_sha256: str | None
    patch_validator_valid: bool | None
    patch_validator_checks: dict[str, str] = Field(default_factory=dict)
    patch_validator_violations: list[str] = Field(default_factory=list)


class StateEvent(ExperimentModel):
    from_state: str
    to_state: str
    at: str
    reason: str


class ApprovalSection(ExperimentModel):
    status: str
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reasons: list[str]
    state_history: list[StateEvent]


class ValidationCheckRow(ExperimentModel):
    name: str
    required: bool
    status: Literal["PASSED", "FAILED", "INCONCLUSIVE", "NOT_RUN"]
    detail: str
    duration_seconds: float | None = None
    failures: list[str] = Field(default_factory=list)


class ValidationSection(ExperimentModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    validation_id: str | None
    status: Literal["PASSED", "FAILED", "INCONCLUSIVE", "NOT_RUN"]
    summary: str
    required_checks: list[str]
    checks: list[ValidationCheckRow]
    error: str | None = None
    duration_seconds: float | None = None
    baseline_input_hashes: dict[str, str] = Field(default_factory=dict)
    finished_at: datetime | None = None


class VariantStats(ExperimentModel):
    variant: Literal["baseline", "candidate"]
    metric: Literal["wall_ms", "cpu_ms"]
    n: int
    median: float
    mean: float
    min: float
    max: float
    stdev: float | None
    p95: float
    cv_percent: float | None
    small_n_warning: str | None = None


class MemoryComparisonRow(ExperimentModel):
    tool: Literal["tracemalloc", "rss", "ru_maxrss"]
    label: str
    baseline_bytes_median: float | None
    candidate_bytes_median: float | None
    ratio_candidate_to_baseline: float | None
    definition: str


class MemoryComparison(ExperimentModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    runs_per_variant: dict[str, int]
    rows: list[MemoryComparisonRow]
    unavailable: list[str] = Field(default_factory=list)


class SampleRow(ExperimentModel):
    variant: str
    kind: str
    pair_index: int
    position_in_pair: int
    wall_ms: float | None
    cpu_ms: float | None
    tracemalloc_peak_bytes: int | None
    rss_peak_bytes: int | None
    ru_maxrss_bytes: int | None
    ok: bool
    error: str | None = None


class BenchmarkSettings(ExperimentModel):
    size: str
    warmups: int
    runs: int
    memory_runs: int
    seed: int
    order: str
    threshold_percent: float
    bootstrap_resamples: int
    timed_iterations_per_process: int = 1


class BenchmarkSection(ExperimentModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    benchmark_id: str
    status: str
    candidate_kind: str
    candidate_label: str
    settings: BenchmarkSettings
    input_sha256: str | None
    baseline_tree_sha256: str | None
    candidate_tree_sha256: str | None
    stats: list[VariantStats]
    throughput_calls_per_s: dict[str, float | None] = Field(default_factory=dict)
    speedup: float | None
    ci_low: float | None
    ci_high: float | None
    ci_confidence: float | None
    ci_method: str | None
    latency_reduction_percent: float | None
    throughput_speedup: float | None
    cpu_speedup: float | None
    cpu_speedup_unavailable_reason: str | None
    memory: MemoryComparison | None
    classification: str | None
    classification_reason: str | None
    improvement_speedup_threshold: float | None
    regression_speedup_threshold: float | None
    correctness: dict[str, Any] = Field(default_factory=dict)
    formulas: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    samples: list[SampleRow] = Field(default_factory=list)


class SummaryItem(ExperimentModel):
    label: str
    value: str
    provenance: Provenance


class MeasuredVsHypothesis(ExperimentModel):
    measured: list[SummaryItem]
    hypotheses: list[SummaryItem]
    verdict: str


class Reproducibility(ExperimentModel):
    cli: list[str]
    api_calls: list[str]
    notes: list[str] = Field(default_factory=list)


class ExperimentReport(ExperimentModel):
    schema_version: str = EXPERIMENT_SCHEMA_VERSION
    kind: ExperimentKind
    metadata: ExperimentMetadata
    outcome: Outcome
    summary_box: MeasuredVsHypothesis
    workload: WorkloadSection
    environment: EnvironmentSection | None
    profiling: ProfilingSection | None
    profiling_absent_reason: str | None = None
    recommendation: RecommendationSection | None = None
    patch: PatchSection | None = None
    approval: ApprovalSection | None = None
    validation: ValidationSection | None = None
    validation_absent_reason: str | None = None
    benchmark: BenchmarkSection | None = None
    benchmark_absent_reason: str | None = None
    reproducibility: Reproducibility
    limitations: list[str] = Field(default_factory=list)
