"""AI_ANALYSIS orchestration: deterministic advisor always, LLM pipeline only with consent.

The LLM path runs only when **all** of these hold: ``settings.llm_configured``, the audit asked for
it (``use_llm``), and the user consented to sending redacted snippets (``llm_consent``). Otherwise
no client is created and nothing leaves the machine. LLM recommendations are labeled
``AI_HYPOTHESIS`` with ``generated_by=<model id>`` and are merged with the deterministic ones by
the priority of the findings they cite.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backend.ai import llm_client
from backend.ai.budget import TokenBudget
from backend.ai.deterministic_advisor import AdvisorOutput, DeterministicAdvisor
from backend.ai.summarizer import ChatClient, HierarchicalPipeline, SummaryCache
from backend.core.config import Settings
from backend.core.exceptions import PerfPilotError
from backend.models.audit_schemas import (
    ArchitectureInsight,
    CodeIndex,
    Finding,
    LLMUsage,
    Recommendation,
)

LLM_NOT_CONFIGURED_WARNING = (
    "AI-assisted analysis was requested, but no LLM endpoint is configured "
    "(PERFPILOT_LLM_BASE_URL and PERFPILOT_LLM_MODEL); recommendations come from the deterministic "
    "advisor and no code left this machine."
)
ClientFactory = Callable[[Settings], ChatClient]
ProgressFn = Callable[[float, str], None]


@dataclass
class AIAnalysisResult:
    recommendations: list[Recommendation] = field(default_factory=list)
    insights: list[ArchitectureInsight] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    warnings: list[str] = field(default_factory=list)
    mode: str = "deterministic"
    diffs_generated: int = 0


def llm_permitted(settings: Settings, use_llm: bool, llm_consent: bool) -> bool:
    return bool(settings.llm_configured and use_llm and llm_consent)


def _merge(
    advice: AdvisorOutput, llm: list[Recommendation], findings: list[Finding]
) -> list[Recommendation]:
    """Interleave by score: a deterministic group's score, or for a model recommendation the best
    priority among the findings it addresses or whose location it cites."""
    ranked: list[tuple[float, int, int, Recommendation]] = []
    for order, (group, rec) in enumerate(zip(advice.groups, advice.recommendations, strict=True)):
        ranked.append((group.score, 0, order, rec))
    for order, rec in enumerate(llm):
        cited = {(c.file_path, line) for c in rec.citations for line in (c.line_start, c.line_end)}
        related = [
            f.priority_score
            for f in findings
            if f.id in rec.finding_ids
            or any(
                path == f.location.file_path
                and f.location.line_start <= line <= f.location.line_end
                for path, line in cited
            )
        ]
        ranked.append((max(related, default=0.0), 1, order, rec))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    merged = []
    for rank, (_, _, _, rec) in enumerate(ranked, start=1):
        merged.append(rec.model_copy(update={"priority_rank": rank}))
    return merged


def run_ai_analysis(
    *,
    findings: list[Finding],
    index: CodeIndex,
    sources: Mapping[str, str],
    settings: Settings,
    use_llm: bool,
    llm_consent: bool,
    root: Path | None = None,
    client_factory: ClientFactory | None = None,
    progress: ProgressFn | None = None,
) -> AIAnalysisResult:
    advice = DeterministicAdvisor(index, sources, root).advise(findings)
    result = AIAnalysisResult(
        recommendations=list(advice.recommendations),
        insights=list(advice.insights),
        usage=LLMUsage(stage_status={"llm": "not_requested"}),
        diffs_generated=advice.diffs_generated,
    )
    if not use_llm:
        return result
    if not llm_permitted(settings, use_llm, llm_consent):
        reason = "no_consent" if settings.llm_configured else "not_configured"
        result.usage.stage_status = {"llm": reason}
        if not settings.llm_configured:
            result.warnings.append(LLM_NOT_CONFIGURED_WARNING)
        return result
    factory = client_factory or llm_client.create_llm_client
    try:
        client = factory(settings)
    except PerfPilotError as exc:
        result.usage.stage_status = {"llm": "unavailable"}
        result.usage.fallbacks += 1
        result.warnings.append(f"AI analysis skipped: {exc.message}")
        return result
    cache_dir = settings.resolved_data_dir / "ai_cache" / "summaries"
    pipeline = HierarchicalPipeline(
        client,
        index=index,
        findings=findings,
        groups=advice.groups,
        sources=dict(sources),
        budget=TokenBudget(settings.llm_max_tokens_per_audit, settings.llm_max_tokens_per_call),
        cache=SummaryCache(cache_dir),
        input_cost_per_1k=settings.llm_input_cost_per_1k,
        output_cost_per_1k=settings.llm_output_cost_per_1k,
        progress=progress,
    )
    try:
        llm = pipeline.run()
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    result.usage = llm.usage
    result.warnings.extend(llm.warnings)
    result.recommendations = _merge(advice, llm.recommendations, findings)
    result.insights = [*advice.insights, *llm.insights]
    result.mode = "deterministic+llm"
    return result
