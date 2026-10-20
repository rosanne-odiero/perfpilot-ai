"""The ``AuditReport`` contract: one model, rendered to HTML, Markdown, JSON, CSV and PDF.

Section numbering follows the Repository Audit feature spec (1-15). Optional measured sections
(7 runtime, 11 validation, 12 benchmarks) are ``None`` unless a report provider supplied data from
a real run; their sub-models pin ``provenance`` to ``MEASURED`` so static or AI output can never be
placed there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.models.audit_schemas import MeasuredEvidence
from backend.models.enums import (
    AuditMode,
    AuditStatus,
    Confidence,
    EffortEstimate,
    FindingCategory,
    Provenance,
    RepositorySourceType,
    Severity,
)

SCHEMA_VERSION = "1.0"

NOT_MEASURED_TEXT = "Not measured — static audit only."

SECTION_TITLES: dict[int, str] = {
    1: "Executive summary",
    2: "Repository metadata & analysis environment",
    3: "Files & modules analyzed",
    4: "Architecture overview & dependency graph",
    5: "Prioritized findings",
    6: "Source references & evidence",
    7: "CPU & memory measurements",
    8: "Algorithmic complexity analysis",
    9: "Proposed optimization strategies",
    10: "Suggested code changes & diffs",
    11: "Correctness validation outcomes",
    12: "Measured benchmark results",
    13: "Estimated engineering effort & uncertainty",
    14: "Optimization roadmap",
    15: "Analysis limitations & reproducibility",
}

MeasuredLabel = Literal[Provenance.MEASURED]


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------
# Metadata and legend
# --------------------------------------------------------------------------------------


class ReportMetadata(ReportModel):
    title: str
    audit_id: str
    generated_at: datetime
    generator: str = Field(description="PerfPilot name and version that produced the report")
    pdf_engine: str | None = Field(default=None, description="Set only on PDF renders")
    providers_applied: list[str] = Field(default_factory=list)
    redactions_applied: int = Field(
        default=0, description="Secret-like values replaced in the report content"
    )


class ProvenanceLegendEntry(ReportModel):
    label: Provenance
    meaning: str
    example: str


PROVENANCE_LEGEND: list[ProvenanceLegendEntry] = [
    ProvenanceLegendEntry(
        label=Provenance.MEASURED,
        meaning="Produced by running code in an isolated PerfPilot environment.",
        example="A profiler or A/B benchmark result recorded for this audit.",
    ),
    ProvenanceLegendEntry(
        label=Provenance.STATIC,
        meaning="Deterministic AST/rule output. No code was executed.",
        example="A rule matched a pattern at a specific file and line.",
    ),
    ProvenanceLegendEntry(
        label=Provenance.AI_HYPOTHESIS,
        meaning="LLM- or heuristic-generated suggestion. Unverified until measured.",
        example="A proposed rewrite and its expected (qualitative) effect.",
    ),
]


# --------------------------------------------------------------------------------------
# 1. Executive summary
# --------------------------------------------------------------------------------------


class KeyCounts(ReportModel):
    files_seen: int
    python_files_analyzed: int
    modules_indexed: int
    findings_total: int
    findings_by_severity: dict[Severity, int]
    findings_by_provenance: dict[Provenance, int]
    recommendations_total: int


class TopRisk(ReportModel):
    finding_id: str
    title: str
    severity: Severity
    provenance: Provenance
    location: str


class ExecutiveSummary(ReportModel):
    text: list[str] = Field(description="Deterministically generated paragraphs")
    counts: KeyCounts
    top_risks: list[TopRisk]


# --------------------------------------------------------------------------------------
# 2. Repository metadata & analysis environment
# --------------------------------------------------------------------------------------


class RepositorySection(ReportModel):
    name: str
    source_type: RepositorySourceType
    source_display: str
    commit_sha: str | None
    short_sha: str | None
    branch: str | None
    requested_ref: str | None
    commit_date: datetime | None
    audit_started_at: datetime
    analyzed_at: datetime
    perfpilot_version: str
    python_version: str
    platform: str
    mode: AuditMode
    audit_status: AuditStatus
    llm_model: str = Field(description="Model id used for AI analysis, or 'none'")
    llm_calls: int = 0


# --------------------------------------------------------------------------------------
# 3. Files & modules analyzed
# --------------------------------------------------------------------------------------


class ParseErrorEntry(ReportModel):
    file_path: str
    error: str


class FilesSection(ReportModel):
    inventory_available: bool
    files_seen: int
    bytes_seen: int
    files_listed: int
    python_files_analyzed: int
    modules_indexed: int
    languages: dict[str, int]
    skipped_total: int
    skipped_by_reason: dict[str, int]
    sensitive_files_withheld: int = Field(
        description="Files matching sensitive-name patterns; their content is never reported"
    )
    limits_hit: list[str]
    parse_errors: list[ParseErrorEntry]


# --------------------------------------------------------------------------------------
# 4. Architecture overview & dependency graph
# --------------------------------------------------------------------------------------


class ModuleRank(ReportModel):
    module_name: str
    file_path: str
    fan_in: int
    fan_out: int
    finding_count: int
    is_entrypoint: bool
    in_cycle: bool


class EntrypointEntry(ReportModel):
    module_name: str
    file_path: str
    reasons: list[str]


class TechnologyEntry(ReportModel):
    name: str
    kind: str


class InsightEntry(ReportModel):
    title: str
    description: str
    modules: list[str]
    provenance: Provenance
    generated_by: str


class EdgeEntry(ReportModel):
    source: str
    target: str
    in_cycle: bool = False


class GraphImage(ReportModel):
    svg: str
    shown_nodes: int
    total_nodes: int
    note: str | None


class ArchitectureSection(ReportModel):
    index_available: bool
    node_count: int
    edge_count: int
    external_import_count: int
    entrypoints: list[EntrypointEntry]
    top_fan_in: list[ModuleRank]
    cycles: list[list[str]]
    technologies: list[TechnologyEntry]
    top_external_imports: dict[str, int]
    insights: list[InsightEntry]
    key_edges: list[EdgeEntry] = Field(
        default_factory=list,
        description="Import edges among the top fan-in modules and entrypoints",
    )
    graph: GraphImage | None
    provenance: Provenance = Provenance.STATIC


# --------------------------------------------------------------------------------------
# 5. Prioritized findings / 6. Evidence
# --------------------------------------------------------------------------------------


class FindingEntry(ReportModel):
    rank: int
    id: str
    rule_id: str
    title: str
    category: FindingCategory
    severity: Severity
    confidence: Confidence
    provenance: Provenance
    priority_score: float
    file_path: str
    line_start: int
    line_end: int
    symbol: str | None
    location: str = Field(description="file:line or file:start-end")
    description: str
    suggestion: str
    tradeoffs: str
    measured: MeasuredEvidence | None


class FindingsSection(ReportModel):
    total: int
    by_severity: dict[Severity, int]
    by_category: dict[FindingCategory, int]
    entries: list[FindingEntry]


class SnippetLine(ReportModel):
    number: int
    text: str


class EvidenceEntry(ReportModel):
    finding_id: str
    rank: int
    title: str
    provenance: Provenance
    location: str
    permalink: str | None
    lines: list[SnippetLine]
    truncated: bool = False
    withheld_reason: str | None = None


class EvidenceSection(ReportModel):
    permalinks_available: bool
    entries: list[EvidenceEntry]


# --------------------------------------------------------------------------------------
# 7. CPU & memory (optional, MEASURED)
# --------------------------------------------------------------------------------------


class RuntimeHotspot(ReportModel):
    function_name: str
    file_path: str | None = None
    line: int | None = None
    internal_time_ms: float
    cumulative_time_ms: float
    call_count: int
    percentage_of_profiled_time: float
    finding_ids: list[str] = Field(default_factory=list)


class RuntimeProfile(ReportModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    profile_id: str
    command: list[str] = Field(description="Exact approved argv that was executed")
    profiler: str
    wall_time_ms: float
    cpu_time_ms: float | None = None
    peak_rss_mb: float | None = None
    peak_traced_memory_mb: float | None = None
    hotspots: list[RuntimeHotspot] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class RuntimeSection(ReportModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    profiles: list[RuntimeProfile]


# --------------------------------------------------------------------------------------
# 8. Complexity
# --------------------------------------------------------------------------------------


class ComplexityEntry(ReportModel):
    finding_id: str
    rank: int
    title: str
    location: str
    estimate: str
    provenance: Provenance


class ComplexitySection(ReportModel):
    entries: list[ComplexityEntry]
    note: str


# --------------------------------------------------------------------------------------
# 9. Strategies / 10. Code changes
# --------------------------------------------------------------------------------------


class StrategyEntry(ReportModel):
    rank: int
    id: str
    title: str
    summary: str
    rationale: str
    proposed_strategy: str
    expected_effect: str
    correctness_risks: str
    finding_ids: list[str]
    citations: list[str]
    provenance: Provenance
    effort: EffortEstimate
    uncertainty: str
    generated_by: str


class StrategiesSection(ReportModel):
    entries: list[StrategyEntry]


class DiffEntry(ReportModel):
    recommendation_id: str
    title: str
    provenance: Provenance
    files: list[str]
    diff: str | None
    truncated: bool = False
    withheld_reason: str | None = None


class CodeChangesSection(ReportModel):
    entries: list[DiffEntry]
    note: str


# --------------------------------------------------------------------------------------
# 11. Validation (optional, MEASURED)
# --------------------------------------------------------------------------------------

ValidationStatus = Literal["PASSED", "FAILED", "INCONCLUSIVE", "NOT_RUN"]


class ValidationCheck(ReportModel):
    name: str
    status: ValidationStatus
    detail: str = ""


class ValidationOutcome(ReportModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    validation_id: str
    recommendation_id: str | None = None
    status: ValidationStatus
    command: list[str] = Field(default_factory=list)
    checks: list[ValidationCheck] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)


class ValidationSection(ReportModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    outcomes: list[ValidationOutcome]


# --------------------------------------------------------------------------------------
# 12. Benchmarks (optional, MEASURED)
# --------------------------------------------------------------------------------------

BenchmarkClassification = Literal[
    "IMPROVED", "REGRESSED", "NO_CLEAR_DIFFERENCE", "INCONCLUSIVE", "INVALID"
]


class BenchmarkSample(ReportModel):
    variant: Literal["baseline", "candidate"]
    run_index: int
    wall_time_ms: float
    peak_memory_mb: float | None = None


class BenchmarkResult(ReportModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    benchmark_id: str
    name: str
    recommendation_id: str | None = None
    baseline_median_ms: float
    candidate_median_ms: float
    speedup: float = Field(description="baseline_median / candidate_median")
    speedup_ci_low: float | None = None
    speedup_ci_high: float | None = None
    confidence_level: float | None = Field(default=None, description="e.g. 0.95")
    classification: BenchmarkClassification
    sample_count: int = Field(description="Measured runs per variant")
    warmup_runs: int = 0
    baseline_peak_memory_mb: float | None = None
    candidate_peak_memory_mb: float | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    samples: list[BenchmarkSample] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BenchmarkSection(ReportModel):
    provenance: MeasuredLabel = Provenance.MEASURED
    results: list[BenchmarkResult]
    chart_svg: str | None = None


# --------------------------------------------------------------------------------------
# 13. Effort / 14. Roadmap
# --------------------------------------------------------------------------------------


class EffortEntry(ReportModel):
    recommendation_id: str
    title: str
    effort: EffortEstimate
    uncertainty: str
    provenance: Provenance


class EffortSection(ReportModel):
    entries: list[EffortEntry]
    totals: dict[EffortEstimate, int]
    scale: dict[EffortEstimate, str]
    note: str


RoadmapPhaseName = Literal["Quick wins", "Next", "Strategic"]


class RoadmapItem(ReportModel):
    recommendation_id: str
    title: str
    effort: EffortEstimate
    provenance: Provenance
    priority_rank: int
    highest_severity: Severity | None
    reason: str


class RoadmapPhase(ReportModel):
    name: RoadmapPhaseName
    description: str
    items: list[RoadmapItem]


class RoadmapSection(ReportModel):
    phases: list[RoadmapPhase]
    rules: list[str]


# --------------------------------------------------------------------------------------
# 15. Limitations & reproducibility
# --------------------------------------------------------------------------------------


class Reproducibility(ReportModel):
    commit_sha: str | None
    source_display: str
    requested_ref: str | None
    perfpilot_version: str
    configuration: dict[str, str]
    environment: dict[str, str]
    commands: list[str]


class LimitationsSection(ReportModel):
    limitations: list[str]
    reproducibility: Reproducibility


# --------------------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------------------


class AuditReport(ReportModel):
    schema_version: str = SCHEMA_VERSION
    metadata: ReportMetadata
    legend: list[ProvenanceLegendEntry] = Field(default_factory=lambda: list(PROVENANCE_LEGEND))
    executive_summary: ExecutiveSummary
    repository: RepositorySection
    files: FilesSection
    architecture: ArchitectureSection
    findings: FindingsSection
    evidence: EvidenceSection
    runtime: RuntimeSection | None = None
    complexity: ComplexitySection
    strategies: StrategiesSection
    code_changes: CodeChangesSection
    validation: ValidationSection | None = None
    benchmarks: BenchmarkSection | None = None
    effort: EffortSection
    roadmap: RoadmapSection
    limitations: LimitationsSection

    @property
    def is_static_only(self) -> bool:
        return self.runtime is None and self.validation is None and self.benchmarks is None
