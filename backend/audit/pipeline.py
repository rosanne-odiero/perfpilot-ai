"""Ordered audit stage pipeline.

Each stage is a plain function over a shared :class:`AuditState`. The static audit is
``STATIC_STAGES``; later phases insert stages (AI analysis, runtime discovery/profiling,
correlation) by building a new tuple, e.g. ``(*STATIC_STAGES, ai_stage)``, without touching the
existing ones. Callers observe stage boundaries through ``on_stage`` (the job handler uses it to
persist ``AuditRun.stage``).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from backend.audit.prioritization import PriorityContext, prioritize
from backend.audit.rule_engine import RuleRunResult, run_rules
from backend.core.config import Settings
from backend.indexing.code_index import IndexBuild, index_repository, link_repository
from backend.models.audit_schemas import Finding, RepositoryInventory
from backend.models.enums import AuditStage

if TYPE_CHECKING:
    from backend.ai.analysis import AIAnalysisResult

ProgressCallback = Callable[[AuditStage, float, str | None], None]


@dataclass
class AuditState:
    root: Path
    inventory: RepositoryInventory
    settings: Settings
    progress: ProgressCallback | None = None
    build: IndexBuild | None = None
    rules: RuleRunResult | None = None
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stage_seconds: dict[str, float] = field(default_factory=dict)
    current_stage: AuditStage = AuditStage.QUEUED
    position: tuple[int, int] = (0, 1)
    use_llm: bool = False
    llm_consent: bool = False
    ai: AIAnalysisResult | None = None

    def report(self, fraction: float, message: str | None = None) -> None:
        """Progress within the current stage (0..1), mapped onto the whole pipeline."""
        if self.progress is None:
            return
        index, total = self.position
        overall = (index + max(0.0, min(1.0, fraction))) / total
        self.progress(self.current_stage, overall, message)

    def require_build(self) -> IndexBuild:
        if self.build is None:
            raise RuntimeError("the INDEXING stage must run before this stage")
        return self.build


StageFn = Callable[[AuditState], None]


@dataclass(frozen=True)
class PipelineStage:
    stage: AuditStage
    run: StageFn
    label: str


def index_stage(state: AuditState) -> None:
    files = [f.path for f in state.inventory.python_files]
    state.build = index_repository(
        state.root,
        files,
        max_files=state.settings.max_python_files,
        max_file_bytes=state.settings.max_file_bytes,
        progress=lambda done, total: state.report(done / total, f"parsed {done}/{total} files"),
    )


def graph_stage(state: AuditState) -> None:
    build = state.require_build()
    link_repository(build)
    state.report(1.0, f"{len(build.index.graph.edges)} internal import edges")


def rules_stage(state: AuditState) -> None:
    build = state.require_build()
    state.rules = run_rules(
        build.parsed.values(),
        progress=lambda done, total: state.report(done / total, f"checked {done}/{total} modules"),
    )


def prioritize_stage(state: AuditState) -> None:
    build = state.require_build()
    if state.rules is None:
        raise RuntimeError("the STATIC_RULES stage must run before PRIORITIZING")
    context = PriorityContext.build(build.index, build.import_graph, build.call_graph, build.parsed)
    state.findings = prioritize(state.rules.findings, context)
    counts: dict[str, int] = {}
    module_by_path = {m.file_path: m.module_name for m in build.index.modules}
    for finding in state.findings:
        module = module_by_path.get(finding.location.file_path)
        if module:
            counts[module] = counts.get(module, 0) + 1
    for node in build.index.graph.nodes:
        node.finding_count = counts.get(node.module_name, 0)


STATIC_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage(AuditStage.INDEXING, index_stage, "Parsing files and extracting symbols"),
    PipelineStage(AuditStage.GRAPHING, graph_stage, "Resolving imports and building graphs"),
    PipelineStage(AuditStage.STATIC_RULES, rules_stage, "Running deterministic rules"),
    PipelineStage(AuditStage.PRIORITIZING, prioritize_stage, "Scoring and ranking findings"),
)


def run_pipeline(
    state: AuditState,
    stages: Sequence[PipelineStage],
    on_stage: Callable[[PipelineStage], None] | None = None,
) -> None:
    for index, stage in enumerate(stages):
        state.current_stage = stage.stage
        state.position = (index, len(stages))
        if on_stage is not None:
            on_stage(stage)
        state.report(0.0, stage.label)
        started = time.perf_counter()
        stage.run(state)
        state.stage_seconds[stage.stage.value] = round(time.perf_counter() - started, 4)
