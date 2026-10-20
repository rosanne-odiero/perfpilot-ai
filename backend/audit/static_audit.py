"""Static audit core: index -> graph -> rules -> prioritization. Pure (no database access)."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from backend.audit.pipeline import STATIC_STAGES, AuditState, ProgressCallback, run_pipeline
from backend.core.config import Settings
from backend.models.audit_schemas import CodeIndex, Finding, RepositoryInventory

MAX_LISTED_WARNINGS = 20


class StaticAuditStats(BaseModel):
    python_files_in_inventory: int = 0
    files_analyzed: int = 0
    files_skipped: int = 0
    skipped_reasons: dict[str, int] = Field(default_factory=dict)
    parse_errors: int = 0
    rule_errors: int = 0
    modules: int = 0
    symbols: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    cycles: int = 0
    entrypoint_modules: int = 0
    entrypoint_symbols: int = 0
    call_graph_edges: int = 0
    import_roots: list[str] = Field(default_factory=list)
    findings_total: int = 0
    suppressed_findings: int = 0
    by_rule: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_confidence: dict[str, int] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    stage_seconds: dict[str, float] = Field(default_factory=dict)


class StaticAuditResult(BaseModel):
    index: CodeIndex
    findings: list[Finding]
    stats: StaticAuditStats
    warnings: list[str] = Field(default_factory=list)
    limits_hit: list[str] = Field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        return bool(self.limits_hit or self.stats.parse_errors or self.stats.rule_errors)


def _count(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def summarize(state: AuditState, duration: float) -> StaticAuditResult:
    """Collect stats, warnings and limits from a pipeline state that ran the static stages."""
    build = state.require_build()
    rules = state.rules
    index = build.index
    inventory_python = [f for f in state.inventory.files if f.is_python]
    skipped_reasons = Counter(build.skipped.values())
    for info in inventory_python:
        if info.skipped:
            skipped_reasons[f"inventory: {info.skip_reason or 'skipped'}"] += 1
    parse_errors = index.parse_errors
    rule_errors = rules.errors if rules else {}
    warnings = list(state.warnings)
    if parse_errors:
        listed = ", ".join(list(parse_errors)[:MAX_LISTED_WARNINGS])
        warnings.append(f"{len(parse_errors)} file(s) could not be parsed: {listed}")
    warnings.extend(f"{path}: {error}" for path, error in rule_errors.items())
    warnings.extend(build.warnings[:MAX_LISTED_WARNINGS])
    if len(build.warnings) > MAX_LISTED_WARNINGS:
        warnings.append(f"+{len(build.warnings) - MAX_LISTED_WARNINGS} more decoding warnings")
    limits = [*state.inventory.limits_hit, *build.limits_hit]
    findings = state.findings
    stats = StaticAuditStats(
        python_files_in_inventory=len(inventory_python),
        files_analyzed=len(build.parsed) - len(parse_errors),
        files_skipped=sum(skipped_reasons.values()),
        skipped_reasons=dict(sorted(skipped_reasons.items())),
        parse_errors=len(parse_errors),
        rule_errors=len(rule_errors),
        modules=len(index.modules),
        symbols=sum(len(m.symbols) for m in index.modules),
        graph_nodes=len(index.graph.nodes),
        graph_edges=len(index.graph.edges),
        cycles=len(index.graph.cycles),
        entrypoint_modules=sum(m.is_entrypoint for m in index.modules),
        entrypoint_symbols=sum(s.is_entrypoint for m in index.modules for s in m.symbols),
        call_graph_edges=build.call_graph.number_of_edges(),
        import_roots=index.import_roots,
        findings_total=len(findings),
        suppressed_findings=rules.suppressed if rules else 0,
        by_rule=_count([f.rule_id for f in findings]),
        by_category=_count([f.category.value for f in findings]),
        by_severity=_count([f.severity.value for f in findings]),
        by_confidence=_count([f.confidence.value for f in findings]),
        duration_seconds=round(duration, 4),
        stage_seconds=dict(state.stage_seconds),
    )
    return StaticAuditResult(
        index=index, findings=findings, stats=stats, warnings=warnings, limits_hit=limits
    )


def run_static_audit(
    root: Path,
    inventory: RepositoryInventory,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> StaticAuditResult:
    """Parse, index and analyze the inventory's Python files under ``root``. Never imports or
    executes repository code."""
    started = time.perf_counter()
    state = AuditState(root=root, inventory=inventory, settings=settings, progress=progress)
    run_pipeline(state, STATIC_STAGES)
    return summarize(state, time.perf_counter() - started)
