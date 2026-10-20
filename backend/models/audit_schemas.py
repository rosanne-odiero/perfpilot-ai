"""Pydantic contracts shared by the Repository Audit pipeline.

These models are the interfaces between ingestion -> indexing -> rules -> advisor -> reporting.
Persisted rows store them as JSON (``model_dump(mode="json")``), so keep changes additive.
All paths are POSIX-style and relative to the repository root.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.enums import (
    Confidence,
    EffortEstimate,
    FindingCategory,
    Provenance,
    RepositorySourceType,
    Severity,
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


# --------------------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------------------


class RepositoryRef(Contract):
    """User-supplied pointer to a repository. Exactly one locator must be set per type."""

    source_type: RepositorySourceType
    url: str | None = None
    ref: str | None = Field(default=None, description="Branch, tag or commit for git sources")
    local_path: str | None = None
    upload_artifact_id: str | None = None
    display_name: str | None = None

    @model_validator(mode="after")
    def _check_locator(self) -> RepositoryRef:
        required = {
            RepositorySourceType.GITHUB_PUBLIC: self.url,
            RepositorySourceType.GITHUB_PRIVATE: self.url,
            RepositorySourceType.ZIP_UPLOAD: self.upload_artifact_id,
            RepositorySourceType.LOCAL_DIRECTORY: self.local_path,
        }[self.source_type]
        if not required:
            raise ValueError(f"missing locator for source type {self.source_type}")
        return self


class GitMetadata(Contract):
    commit_sha: str | None = None
    branch: str | None = None
    remote_url: str | None = Field(default=None, description="Credentials always stripped")
    commit_date: datetime | None = None
    is_dirty: bool | None = None


class SourceFileInfo(Contract):
    path: str
    size_bytes: int
    sha256: str | None = None
    language: str | None = None
    is_python: bool = False
    skipped: bool = False
    skip_reason: str | None = None


class DetectedTechnology(Contract):
    name: str
    kind: Literal["language", "framework", "library", "tool", "database", "runtime"]
    evidence: list[str] = Field(default_factory=list)


class DependencySpec(Contract):
    name: str
    version_spec: str | None = None
    source_file: str
    group: str | None = None


class RepositoryInventory(Contract):
    root_name: str
    files: list[SourceFileInfo] = Field(default_factory=list)
    total_files_seen: int = 0
    total_bytes_seen: int = 0
    languages: dict[str, int] = Field(default_factory=dict)
    technologies: list[DetectedTechnology] = Field(default_factory=list)
    dependencies: list[DependencySpec] = Field(default_factory=list)
    limits_hit: list[str] = Field(default_factory=list)

    @property
    def python_files(self) -> list[SourceFileInfo]:
        return [f for f in self.files if f.is_python and not f.skipped]


class AcquiredSource(Contract):
    """Result of a provider: a local, read-only working copy plus metadata."""

    workspace_path: str
    source_type: RepositorySourceType
    display_name: str
    git: GitMetadata = Field(default_factory=GitMetadata)


# --------------------------------------------------------------------------------------
# Indexing
# --------------------------------------------------------------------------------------


class CodeLocation(Contract):
    file_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> CodeLocation:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class SymbolInfo(Contract):
    qualname: str
    kind: Literal["function", "async_function", "method", "async_method", "class"]
    file_path: str
    line_start: int
    line_end: int
    decorators: list[str] = Field(default_factory=list)
    max_loop_depth: int = 0
    is_recursive: bool = False
    calls: list[str] = Field(default_factory=list, description="Best-effort callee names")
    docstring: str | None = None


class ImportRef(Contract):
    module: str
    names: list[str] = Field(default_factory=list)
    line: int
    level: int = 0
    resolved_module: str | None = None
    is_internal: bool = False


class ModuleInfo(Contract):
    module_name: str
    file_path: str
    is_package: bool = False
    line_count: int = 0
    imports: list[ImportRef] = Field(default_factory=list)
    symbols: list[SymbolInfo] = Field(default_factory=list)
    is_entrypoint: bool = False
    entrypoint_reasons: list[str] = Field(default_factory=list)
    parse_error: str | None = None


class GraphNode(Contract):
    module_name: str
    file_path: str
    fan_in: int = 0
    fan_out: int = 0
    is_entrypoint: bool = False
    finding_count: int = 0
    in_cycle: bool = False


class GraphEdge(Contract):
    source: str
    target: str
    line: int | None = None


class DependencyGraph(Contract):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    external_imports: dict[str, int] = Field(default_factory=dict)
    cycles: list[list[str]] = Field(default_factory=list)


class CodeIndex(Contract):
    modules: list[ModuleInfo] = Field(default_factory=list)
    graph: DependencyGraph = Field(default_factory=DependencyGraph)
    parse_errors: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Findings and recommendations
# --------------------------------------------------------------------------------------


class MeasuredEvidence(Contract):
    """Runtime evidence attached to a finding when a profiler confirms it."""

    profile_id: str
    function_name: str
    internal_time_ms: float
    cumulative_time_ms: float
    call_count: int
    percentage_of_profiled_time: float
    note: str | None = None


class Finding(Contract):
    id: str
    rule_id: str
    title: str
    category: FindingCategory
    description: str
    severity: Severity
    confidence: Confidence
    provenance: Provenance = Provenance.STATIC
    location: CodeLocation
    snippet: str
    suggestion: str
    tradeoffs: str
    complexity_note: str | None = None
    priority_score: float = 0.0
    priority_factors: dict[str, float] = Field(default_factory=dict)
    measured: MeasuredEvidence | None = None


class Recommendation(Contract):
    id: str
    title: str
    summary: str
    rationale: str
    proposed_strategy: str
    finding_ids: list[str] = Field(default_factory=list)
    citations: list[CodeLocation] = Field(default_factory=list)
    provenance: Provenance = Provenance.STATIC
    effort: EffortEstimate = EffortEstimate.MEDIUM
    uncertainty: str
    expected_effect: str = Field(description="Qualitative hypothesis, never a measured number")
    correctness_risks: str = ""
    suggested_diff: str | None = None
    priority_rank: int = 0
    generated_by: str = Field(default="deterministic", description="'deterministic' or model id")


class ArchitectureInsight(Contract):
    id: str
    title: str
    description: str
    modules: list[str] = Field(default_factory=list)
    citations: list[CodeLocation] = Field(default_factory=list)
    provenance: Provenance = Provenance.STATIC
    generated_by: str = "deterministic"


class LLMUsage(Contract):
    model: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    budget_tokens: int = 0
    budget_exhausted: bool = False
    rejected_citations: int = 0
    fallbacks: int = 0
