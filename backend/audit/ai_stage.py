"""AI_ANALYSIS stage: recommendations and insights after PRIORITIZING (see ``backend.ai``)."""

from __future__ import annotations

from backend.ai.analysis import run_ai_analysis
from backend.audit.pipeline import AuditState, PipelineStage
from backend.models.enums import AuditStage


def ai_analysis_stage(state: AuditState) -> None:
    build = state.require_build()
    sources = {path: module.source for path, module in build.parsed.items() if module.tree}
    state.ai = run_ai_analysis(
        findings=state.findings,
        index=build.index,
        sources=sources,
        settings=state.settings,
        use_llm=state.use_llm,
        llm_consent=state.llm_consent,
        root=state.root,
        progress=state.report,
    )
    state.warnings.extend(state.ai.warnings)


AI_STAGE = PipelineStage(
    AuditStage.AI_ANALYSIS, ai_analysis_stage, "Generating recommendations and insights"
)
