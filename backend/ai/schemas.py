"""Pydantic models for the structured outputs of each LLM pipeline step.

The model is asked for JSON matching these schemas. Anything that does not validate is retried a
bounded number of times and then replaced by the deterministic result. Validated outputs still go
through :mod:`backend.ai.citation_guard` before anything is accepted.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.models.enums import EffortEstimate


class LLMOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class Citation(LLMOutput):
    file_path: str = Field(min_length=1, max_length=1024)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol: str | None = Field(default=None, max_length=512)


class UnitSummary(LLMOutput):
    unit_id: str = Field(description="Exactly one of the provided unit ids (module:qualname)")
    summary: str = Field(min_length=1, max_length=1200)
    performance_notes: str = Field(default="", max_length=1200)


class UnitSummaries(LLMOutput):
    summaries: list[UnitSummary] = Field(default_factory=list, max_length=50)


class ModuleContext(LLMOutput):
    module: str = Field(description="Exactly one of the provided module names")
    role: str = Field(min_length=1, max_length=800)
    concerns: str = Field(default="", max_length=800)


class DependencyContext(LLMOutput):
    modules: list[ModuleContext] = Field(default_factory=list, max_length=40)


class HotPath(LLMOutput):
    units: list[str] = Field(min_length=1, max_length=12, description="Ordered unit ids")
    rationale: str = Field(min_length=1, max_length=1200)
    citations: list[Citation] = Field(default_factory=list, max_length=12)


class HotPaths(LLMOutput):
    paths: list[HotPath] = Field(default_factory=list, max_length=10)


class InsightDraft(LLMOutput):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2500)
    modules: list[str] = Field(default_factory=list, max_length=20)
    citations: list[Citation] = Field(default_factory=list, max_length=12)


class Correlations(LLMOutput):
    insights: list[InsightDraft] = Field(default_factory=list, max_length=10)


class RecommendationDraft(LLMOutput):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1500)
    rationale: str = Field(min_length=1, max_length=3000)
    proposed_strategy: str = Field(min_length=1, max_length=4000)
    finding_ids: list[str] = Field(default_factory=list, max_length=50)
    citations: list[Citation] = Field(min_length=1, max_length=12)
    effort: EffortEstimate = EffortEstimate.MEDIUM
    uncertainty: str = Field(min_length=1, max_length=1500)
    expected_effect: str = Field(min_length=1, max_length=1500)
    correctness_risks: str = Field(default="", max_length=2000)


class RecommendationDrafts(LLMOutput):
    recommendations: list[RecommendationDraft] = Field(default_factory=list, max_length=10)
