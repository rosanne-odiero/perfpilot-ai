"""Build an ``AuditReport`` from the database.

Everything here is deterministic: numbers come only from persisted audit data (inventory, code
index, findings, recommendations) or from report providers that read measured runs. Nothing is
estimated or invented; missing data is reported as missing.
"""

from __future__ import annotations

import platform
import shlex
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.core.exceptions import ConflictError, NotFoundError
from backend.core.redaction import is_sensitive_path
from backend.models.audit_schemas import (
    ArchitectureInsight,
    CodeIndex,
    DependencyGraph,
    Finding,
    GraphNode,
    LLMUsage,
    Recommendation,
    RepositoryInventory,
)
from backend.models.enums import (
    AuditMode,
    AuditStatus,
    EffortEstimate,
    FindingCategory,
    Provenance,
    RepositorySourceType,
    Severity,
)
from backend.models.orm.audit import (
    AuditRun,
    FindingRecord,
    RecommendationRecord,
    Repository,
    RepositorySnapshot,
)
from backend.reporting.charts import benchmark_medians_svg
from backend.reporting.graph_svg import DEFAULT_MAX_NODES, render_dependency_graph
from backend.reporting.model import (
    ArchitectureSection,
    AuditReport,
    BenchmarkSection,
    CodeChangesSection,
    ComplexityEntry,
    ComplexitySection,
    DiffEntry,
    EdgeEntry,
    EffortEntry,
    EffortSection,
    EntrypointEntry,
    EvidenceEntry,
    EvidenceSection,
    ExecutiveSummary,
    FilesSection,
    FindingEntry,
    FindingsSection,
    GraphImage,
    InsightEntry,
    KeyCounts,
    LimitationsSection,
    ModuleRank,
    ParseErrorEntry,
    ReportMetadata,
    RepositorySection,
    Reproducibility,
    RuntimeSection,
    StrategiesSection,
    StrategyEntry,
    TechnologyEntry,
    TopRisk,
    ValidationSection,
)
from backend.reporting.provider_registry import (
    ProviderContext,
    ProviderContribution,
    registered_providers,
)
from backend.reporting.roadmap import build_roadmap
from backend.reporting.sources import (
    SENSITIVE_WITHHELD,
    diff_files,
    format_location,
    github_base,
    github_permalink,
    snippet_lines,
    touches_sensitive_file,
    truncate_diff,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

REPORTABLE_STATUSES = {AuditStatus.SUCCEEDED, AuditStatus.PARTIAL}
TOP_RISKS = 5
TOP_FAN_IN = 10
KEY_EDGE_MODULES = 25
KEY_EDGES = 60

EFFORT_SCALE: dict[EffortEstimate, str] = {
    EffortEstimate.SMALL: "Localized change in one function or file; existing tests likely "
    "cover it.",
    EffortEstimate.MEDIUM: "Changes across a module, or new tests/fixtures are needed.",
    EffortEstimate.LARGE: "Cross-module or architectural change; needs design and a measured "
    "baseline.",
}

STATIC_LIMITATIONS = [
    "Findings are static hypotheses derived from source patterns. They indicate where to look, "
    "not proven bottlenecks; their real impact depends on data sizes and call frequency.",
    "No repository code was executed during the static audit. No runtime, memory or benchmark "
    "numbers are reported unless a section is explicitly labeled MEASURED.",
    "Complexity notes are estimates from code structure, not measurements.",
    "Dynamic imports, plugins, reflection and code generated at runtime are not visible to the "
    "static dependency graph.",
]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _severity_counts(findings: Iterable[Finding]) -> dict[Severity, int]:
    counts = Counter(f.severity for f in findings)
    return {s: counts.get(s, 0) for s in Severity}


def _provenance_counts(findings: Iterable[Finding]) -> dict[Provenance, int]:
    counts = Counter(f.provenance for f in findings)
    return {p: counts.get(p, 0) for p in Provenance}


def _parse_model(model: type[ModelT], data: Any, label: str, problems: list[str]) -> ModelT | None:
    if not data:
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems.append(f"The stored {label} could not be parsed ({exc.error_count()} errors).")
        return None


class _AuditData:
    """All rows needed for one report, loaded once."""

    def __init__(self, session: Session, audit_id: str) -> None:
        audit = session.get(AuditRun, audit_id)
        if audit is None:
            raise NotFoundError(f"audit {audit_id} not found")
        if audit.status not in REPORTABLE_STATUSES:
            raise ConflictError(
                f"audit {audit_id} is not finished (status {audit.status})",
                details={"audit_id": audit_id, "status": audit.status},
            )
        snapshot = session.get(RepositorySnapshot, audit.snapshot_id)
        if snapshot is None:
            raise NotFoundError(f"snapshot {audit.snapshot_id} not found")
        repository = session.get(Repository, snapshot.repository_id)
        if repository is None:
            raise NotFoundError(f"repository {snapshot.repository_id} not found")
        self.audit = audit
        self.snapshot = snapshot
        self.repository = repository
        self.problems: list[str] = []
        self.inventory = _parse_model(
            RepositoryInventory, snapshot.inventory, "inventory", self.problems
        )
        self.index = _parse_model(CodeIndex, audit.code_index, "code index", self.problems)
        self.llm_usage = (
            _parse_model(LLMUsage, audit.llm_usage, "LLM usage", self.problems) or LLMUsage()
        )
        self.insights = [
            insight
            for raw in audit.insights or []
            if (insight := _parse_model(ArchitectureInsight, raw, "insight", self.problems))
        ]
        finding_rows = session.scalars(
            select(FindingRecord).where(FindingRecord.audit_id == audit.id)
        ).all()
        findings = [
            f
            for row in finding_rows
            if (f := _parse_model(Finding, row.data, "finding", self.problems))
        ]
        self.findings = sorted(
            findings,
            key=lambda f: (
                -f.priority_score,
                -f.severity.weight,
                f.location.file_path,
                f.location.line_start,
                f.id,
            ),
        )
        rec_rows = session.scalars(
            select(RecommendationRecord).where(RecommendationRecord.audit_id == audit.id)
        ).all()
        recs = [
            r
            for row in rec_rows
            if (r := _parse_model(Recommendation, row.data, "recommendation", self.problems))
        ]
        self.recommendations = sorted(
            recs, key=lambda r: (r.priority_rank <= 0, r.priority_rank, r.id)
        )
        self.source_type = RepositorySourceType(repository.source_type)
        self.commit_sha = snapshot.commit_sha or (snapshot.git or {}).get("commit_sha")


def _llm_model_display(data: _AuditData) -> str:
    usage = data.llm_usage
    if data.audit.use_llm and usage.calls > 0 and usage.model:
        return usage.model
    return "none"


def _source_display(data: _AuditData) -> str:
    if data.source_type == RepositorySourceType.ZIP_UPLOAD:
        return f"ZIP upload ({data.repository.name})"
    return data.repository.source_locator


def _repository_section(data: _AuditData) -> RepositorySection:
    git = data.snapshot.git or {}
    commit_date = git.get("commit_date")
    return RepositorySection(
        name=data.repository.name,
        source_type=data.source_type,
        source_display=_source_display(data),
        commit_sha=data.commit_sha,
        short_sha=data.commit_sha[:7] if data.commit_sha else None,
        branch=data.snapshot.branch or git.get("branch"),
        requested_ref=data.repository.requested_ref,
        commit_date=datetime.fromisoformat(commit_date) if isinstance(commit_date, str) else None,
        audit_started_at=data.audit.created_at,
        analyzed_at=data.audit.updated_at,
        perfpilot_version=__version__,
        python_version=platform.python_version(),
        platform=f"{platform.system()} {platform.machine()}",
        mode=AuditMode(data.audit.mode),
        audit_status=AuditStatus(data.audit.status),
        llm_model=_llm_model_display(data),
        llm_calls=data.llm_usage.calls if data.audit.use_llm else 0,
    )


def _files_section(data: _AuditData) -> FilesSection:
    inv = data.inventory
    index = data.index
    parse_errors: dict[str, str] = dict(index.parse_errors) if index else {}
    if index:
        for module in index.modules:
            if module.parse_error and module.file_path not in parse_errors:
                parse_errors[module.file_path] = module.parse_error
    skipped = [f for f in inv.files if f.skipped] if inv else []
    return FilesSection(
        inventory_available=inv is not None,
        files_seen=inv.total_files_seen if inv else 0,
        bytes_seen=inv.total_bytes_seen if inv else 0,
        files_listed=len(inv.files) if inv else 0,
        python_files_analyzed=len(inv.python_files) if inv else 0,
        modules_indexed=len(index.modules) if index else 0,
        languages=dict(sorted(inv.languages.items(), key=lambda kv: (-kv[1], kv[0])))
        if inv
        else {},
        skipped_total=len(skipped),
        skipped_by_reason=dict(
            sorted(Counter(f.skip_reason or "unspecified" for f in skipped).items())
        ),
        sensitive_files_withheld=sum(1 for f in inv.files if is_sensitive_path(f.path))
        if inv
        else 0,
        limits_hit=list(inv.limits_hit) if inv else [],
        parse_errors=[
            ParseErrorEntry(file_path=path, error=error)
            for path, error in sorted(parse_errors.items())
        ],
    )


def enrich_graph(graph: DependencyGraph, findings: list[Finding]) -> DependencyGraph:
    """Recompute fan-in/out from edges and finding counts from this audit's findings."""
    names = {n.module_name for n in graph.nodes}
    edges = {(e.source, e.target) for e in graph.edges if e.source != e.target}
    fan_in = Counter(t for s, t in edges if s in names and t in names)
    fan_out = Counter(s for s, t in edges if s in names and t in names)
    per_file = Counter(f.location.file_path for f in findings)
    cycle_members = {m for cycle in graph.cycles for m in cycle}
    nodes = [
        GraphNode(
            module_name=n.module_name,
            file_path=n.file_path,
            fan_in=fan_in.get(n.module_name, 0) if edges else n.fan_in,
            fan_out=fan_out.get(n.module_name, 0) if edges else n.fan_out,
            is_entrypoint=n.is_entrypoint,
            finding_count=per_file.get(n.file_path, 0) if findings else n.finding_count,
            in_cycle=n.in_cycle or n.module_name in cycle_members,
        )
        for n in graph.nodes
    ]
    return graph.model_copy(update={"nodes": nodes})


def _key_edges(graph: DependencyGraph, ranked: list[GraphNode]) -> list[EdgeEntry]:
    """Edges among the most depended-upon modules and entrypoints (for compact diagrams)."""
    names = [n.module_name for n in ranked[:TOP_FAN_IN]]
    names += sorted(
        n.module_name for n in graph.nodes if n.is_entrypoint and n.module_name not in names
    )
    keep = set(names[:KEY_EDGE_MODULES])
    cycle_edges = {
        (cycle[i], cycle[(i + 1) % len(cycle)]) for cycle in graph.cycles for i in range(len(cycle))
    }
    pairs = sorted(
        {(e.source, e.target) for e in graph.edges if e.source in keep and e.target in keep}
        - {(m, m) for m in keep}
    )
    return [
        EdgeEntry(source=s, target=t, in_cycle=(s, t) in cycle_edges) for s, t in pairs[:KEY_EDGES]
    ]


def _architecture_section(data: _AuditData) -> ArchitectureSection:
    index = data.index
    technologies = (
        [TechnologyEntry(name=t.name, kind=t.kind) for t in data.inventory.technologies]
        if data.inventory
        else []
    )
    insights = [
        InsightEntry(
            title=i.title,
            description=i.description,
            modules=list(i.modules),
            provenance=i.provenance,
            generated_by=i.generated_by,
        )
        for i in data.insights
    ]
    if index is None:
        return ArchitectureSection(
            index_available=False,
            node_count=0,
            edge_count=0,
            external_import_count=0,
            entrypoints=[],
            top_fan_in=[],
            cycles=[],
            technologies=technologies,
            top_external_imports={},
            insights=insights,
            graph=None,
        )
    graph = enrich_graph(index.graph, data.findings)
    entry_reasons = {m.module_name: m.entrypoint_reasons for m in index.modules}
    ranked = sorted(graph.nodes, key=lambda n: (-n.fan_in, -n.finding_count, n.module_name))
    rendering = render_dependency_graph(graph, max_nodes=DEFAULT_MAX_NODES)
    externals = sorted(graph.external_imports.items(), key=lambda kv: (-kv[1], kv[0]))
    return ArchitectureSection(
        index_available=True,
        node_count=len(graph.nodes),
        edge_count=len({(e.source, e.target) for e in graph.edges}),
        external_import_count=len(graph.external_imports),
        entrypoints=[
            EntrypointEntry(
                module_name=n.module_name,
                file_path=n.file_path,
                reasons=list(entry_reasons.get(n.module_name, [])),
            )
            for n in sorted(graph.nodes, key=lambda n: n.module_name)
            if n.is_entrypoint
        ],
        top_fan_in=[
            ModuleRank(
                module_name=n.module_name,
                file_path=n.file_path,
                fan_in=n.fan_in,
                fan_out=n.fan_out,
                finding_count=n.finding_count,
                is_entrypoint=n.is_entrypoint,
                in_cycle=n.in_cycle,
            )
            for n in ranked[:TOP_FAN_IN]
        ],
        cycles=[list(c) for c in graph.cycles],
        technologies=technologies,
        top_external_imports=dict(externals[:15]),
        insights=insights,
        key_edges=_key_edges(graph, ranked),
        graph=GraphImage(
            svg=rendering.svg,
            shown_nodes=rendering.shown_nodes,
            total_nodes=rendering.total_nodes,
            note=rendering.note,
        )
        if rendering
        else None,
    )


def _finding_entries(findings: list[Finding]) -> list[FindingEntry]:
    return [
        FindingEntry(
            rank=rank,
            id=f.id,
            rule_id=f.rule_id,
            title=f.title,
            category=f.category,
            severity=f.severity,
            confidence=f.confidence,
            provenance=f.provenance,
            priority_score=round(f.priority_score, 3),
            file_path=f.location.file_path,
            line_start=f.location.line_start,
            line_end=f.location.line_end,
            symbol=f.location.symbol,
            location=format_location(f.location),
            description=f.description,
            suggestion=f.suggestion,
            tradeoffs=f.tradeoffs,
            measured=f.measured,
        )
        for rank, f in enumerate(findings, start=1)
    ]


def _evidence_section(data: _AuditData) -> EvidenceSection:
    base = github_base(data.source_type, data.repository.source_locator)
    entries: list[EvidenceEntry] = []
    for rank, f in enumerate(data.findings, start=1):
        sensitive = is_sensitive_path(f.location.file_path)
        lines, truncated = (
            ([], False) if sensitive else snippet_lines(f.snippet, f.location.line_start)
        )
        entries.append(
            EvidenceEntry(
                finding_id=f.id,
                rank=rank,
                title=f.title,
                provenance=f.provenance,
                location=format_location(f.location),
                permalink=github_permalink(base, data.commit_sha, f.location),
                lines=lines,
                truncated=truncated,
                withheld_reason=SENSITIVE_WITHHELD if sensitive else None,
            )
        )
    return EvidenceSection(
        permalinks_available=base is not None and data.commit_sha is not None,
        entries=entries,
    )


def _complexity_section(findings: list[Finding]) -> ComplexitySection:
    entries = [
        ComplexityEntry(
            finding_id=f.id,
            rank=rank,
            title=f.title,
            location=format_location(f.location),
            estimate=f.complexity_note,
            provenance=f.provenance,
        )
        for rank, f in enumerate(findings, start=1)
        if f.complexity_note
    ]
    note = (
        "Complexity figures are estimates derived from code structure (loop nesting, collection "
        "operations). They are not measurements and assume the input sizes implied by the code."
    )
    return ComplexitySection(entries=entries, note=note)


def _strategy_section(recs: list[Recommendation]) -> StrategiesSection:
    return StrategiesSection(
        entries=[
            StrategyEntry(
                rank=rank,
                id=r.id,
                title=r.title,
                summary=r.summary,
                rationale=r.rationale,
                proposed_strategy=r.proposed_strategy,
                expected_effect=r.expected_effect,
                correctness_risks=r.correctness_risks,
                finding_ids=list(r.finding_ids),
                citations=[format_location(c) for c in r.citations],
                provenance=r.provenance,
                effort=r.effort,
                uncertainty=r.uncertainty,
                generated_by=r.generated_by,
            )
            for rank, r in enumerate(recs, start=1)
        ]
    )


def _code_changes_section(recs: list[Recommendation]) -> CodeChangesSection:
    entries: list[DiffEntry] = []
    for r in recs:
        if not r.suggested_diff:
            continue
        files = diff_files(r.suggested_diff)
        if touches_sensitive_file(files):
            entries.append(
                DiffEntry(
                    recommendation_id=r.id,
                    title=r.title,
                    provenance=r.provenance,
                    files=files,
                    diff=None,
                    withheld_reason=SENSITIVE_WITHHELD,
                )
            )
            continue
        diff, truncated = truncate_diff(r.suggested_diff)
        entries.append(
            DiffEntry(
                recommendation_id=r.id,
                title=r.title,
                provenance=r.provenance,
                files=files,
                diff=diff,
                truncated=truncated,
            )
        )
    note = (
        "Diffs are proposals. None has been applied; each must pass correctness validation and an "
        "A/B benchmark before it can be considered an improvement."
        if entries
        else "No code changes were generated for this audit."
    )
    return CodeChangesSection(entries=entries, note=note)


def _effort_section(recs: list[Recommendation]) -> EffortSection:
    totals = Counter(r.effort for r in recs)
    return EffortSection(
        entries=[
            EffortEntry(
                recommendation_id=r.id,
                title=r.title,
                effort=r.effort,
                uncertainty=r.uncertainty,
                provenance=r.provenance,
            )
            for r in recs
        ],
        totals={e: totals.get(e, 0) for e in EffortEstimate},
        scale=dict(EFFORT_SCALE),
        note=(
            "Effort is a qualitative size, not a time estimate. Every expected effect is a "
            "hypothesis until the change is validated and benchmarked."
        ),
    )


def _executive_summary(
    data: _AuditData, files: FilesSection, contributions: ProviderContribution
) -> ExecutiveSummary:
    findings = data.findings
    by_severity = _severity_counts(findings)
    severe = by_severity[Severity.CRITICAL] + by_severity[Severity.HIGH]
    sha = f" at commit {data.commit_sha[:7]}" if data.commit_sha else ""
    paragraphs = [
        f"PerfPilot ran a {data.audit.mode} audit of {data.repository.name}{sha}. "
        f"It examined {files.python_files_analyzed} Python files out of {files.files_seen} files "
        f"seen ({_plural(files.modules_indexed, 'module')} indexed, "
        f"{_plural(len(files.parse_errors), 'parse error')}) "
        f"and produced {_plural(len(findings), 'finding')} ({severe} critical or high severity) "
        f"and {_plural(len(data.recommendations), 'recommendation')}."
    ]
    if findings:
        category_counts = Counter(f.category for f in findings)
        top_categories = sorted(category_counts.items(), key=lambda kv: (-kv[1], kv[0].value))[:3]
        paragraphs.append(
            "The most common finding categories are "
            + ", ".join(f"{c.value.lower().replace('_', ' ')} ({n})" for c, n in top_categories)
            + "."
        )
    else:
        paragraphs.append("No performance findings were reported for the analyzed files.")
    measured_parts = []
    if contributions.runtime_profiles:
        measured_parts.append("runtime profiles")
    if contributions.validation_outcomes:
        measured_parts.append("correctness validation")
    if contributions.benchmark_results:
        measured_parts.append("A/B benchmarks")
    if measured_parts:
        paragraphs.append(
            "This report also includes measured data ("
            + ", ".join(measured_parts)
            + ") from runs recorded for this audit; see the sections labeled accordingly."
        )
    else:
        paragraphs.append(
            "No repository code was executed: every finding is a static or AI-generated "
            "hypothesis that has not been measured."
        )
    if data.audit.status == AuditStatus.PARTIAL or files.limits_hit:
        paragraphs.append(
            "The analysis is partial; see section 15 for the limits that were reached."
        )
    return ExecutiveSummary(
        text=paragraphs,
        counts=KeyCounts(
            files_seen=files.files_seen,
            python_files_analyzed=files.python_files_analyzed,
            modules_indexed=files.modules_indexed,
            findings_total=len(findings),
            findings_by_severity=by_severity,
            findings_by_provenance=_provenance_counts(findings),
            recommendations_total=len(data.recommendations),
        ),
        top_risks=[
            TopRisk(
                finding_id=f.id,
                title=f.title,
                severity=f.severity,
                provenance=f.provenance,
                location=format_location(f.location),
            )
            for f in findings[:TOP_RISKS]
        ],
    )


def _reproduction_commands(data: _AuditData) -> list[str]:
    commands: list[str] = []
    base = github_base(data.source_type, data.repository.source_locator)
    ref = data.commit_sha or data.repository.requested_ref
    if base:
        commands.append(f"git clone {shlex.quote(base)}.git repo")
        if data.commit_sha:
            commands.append(f"git -C repo checkout {shlex.quote(data.commit_sha)}")
    commands.extend(
        [
            "perfpilot init-db",
            "uvicorn backend.main:app --port 8000   # terminal 1",
            "perfpilot worker                       # terminal 2",
        ]
    )
    import_body: dict[str, str] = {"source_type": data.source_type.value}
    if data.source_type in {
        RepositorySourceType.GITHUB_PUBLIC,
        RepositorySourceType.GITHUB_PRIVATE,
    }:
        import_body["url"] = base or data.repository.source_locator
        if ref:
            import_body["ref"] = ref
    elif data.source_type == RepositorySourceType.LOCAL_DIRECTORY:
        import_body["local_path"] = data.repository.source_locator
    if data.source_type == RepositorySourceType.ZIP_UPLOAD:
        commands.append(
            "# upload the same ZIP archive again: POST /api/repositories/upload (multipart)"
        )
    else:
        payload = ", ".join(f'"{k}": "{v}"' for k, v in import_body.items())
        commands.append(
            "curl -X POST http://127.0.0.1:8000/api/repositories "
            f"-H 'Content-Type: application/json' -d '{{{payload}}}'"
        )
    use_llm = "true" if data.audit.use_llm else "false"
    commands.append(
        f"# start a {data.audit.mode} audit of the new snapshot: POST /api/audits "
        f"(use_llm={use_llm})"
    )
    commands.append(
        "curl -X POST http://127.0.0.1:8000/api/audits/<audit_id>/reports "
        '-H \'Content-Type: application/json\' -d \'{"formats": ["html", "json"]}\''
    )
    return commands


def _limitations_section(
    data: _AuditData,
    files: FilesSection,
    architecture: ArchitectureSection,
    contributions: ProviderContribution,
    settings: Settings,
) -> LimitationsSection:
    items = list(STATIC_LIMITATIONS)
    if data.audit.status == AuditStatus.PARTIAL:
        items.append("The audit finished as PARTIAL: some stages did not complete.")
    for limit in files.limits_hit:
        items.append(f"Ingestion limit reached: {limit}. The analysis covers a subset of files.")
    if files.skipped_total:
        items.append(f"{files.skipped_total} files were skipped (see section 3 for the reasons).")
    if files.parse_errors:
        items.append(
            f"{len(files.parse_errors)} Python files could not be parsed and were not analyzed."
        )
    if files.sensitive_files_withheld:
        items.append(
            f"{files.sensitive_files_withheld} sensitive files (e.g. .env, keys) were detected; "
            "their content is never included in reports."
        )
    if not files.inventory_available:
        items.append("No file inventory was stored for this snapshot.")
    if not architecture.index_available:
        items.append("No code index was stored for this audit; the architecture section is empty.")
    elif architecture.graph and architecture.graph.note:
        items.append(
            f"The dependency graph image is capped: {architecture.graph.note.lower()}, "
            "ranked by fan-in, fan-out, findings, entrypoints and cycles."
        )
    usage = data.llm_usage
    if data.audit.use_llm:
        if usage.budget_exhausted:
            items.append(
                "The LLM token budget was exhausted; remaining analysis was deterministic."
            )
        if usage.rejected_citations:
            items.append(
                f"{usage.rejected_citations} AI citations were rejected because they did not "
                "match the indexed source."
            )
        if usage.fallbacks:
            items.append(
                f"The deterministic advisor was used as a fallback {usage.fallbacks} times."
            )
    else:
        items.append("No LLM was used; recommendations come from the deterministic advisor.")
    for warning in data.audit.warnings or []:
        items.append(f"Audit warning: {warning if isinstance(warning, str) else str(warning)}")
    items.extend(data.problems)
    items.extend(contributions.limitations)

    configuration = {
        "mode": str(data.audit.mode),
        "use_llm": str(bool(data.audit.use_llm)).lower(),
        "llm_consent": str(bool(data.audit.llm_consent)).lower(),
        "llm_model": _llm_model_display(data),
        "requested_ref": data.repository.requested_ref or "default branch",
        "max_repo_files (current setting)": str(settings.max_repo_files),
        "max_python_files (current setting)": str(settings.max_python_files),
        "max_file_bytes (current setting)": str(settings.max_file_bytes),
    }
    environment = {
        "perfpilot_version": __version__,
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
    }
    if data.snapshot.content_hash:
        environment["source_content_hash"] = data.snapshot.content_hash
    return LimitationsSection(
        limitations=items,
        reproducibility=Reproducibility(
            commit_sha=data.commit_sha,
            source_display=_source_display(data),
            requested_ref=data.repository.requested_ref,
            perfpilot_version=__version__,
            configuration=configuration,
            environment=environment,
            commands=_reproduction_commands(data),
        ),
    )


def collect_contributions(
    session: Session, data: _AuditData
) -> tuple[ProviderContribution, list[str]]:
    context = ProviderContext(
        audit_id=data.audit.id,
        snapshot_id=data.snapshot.id,
        repository_id=data.repository.id,
        commit_sha=data.commit_sha,
        findings=list(data.findings),
    )
    merged = ProviderContribution()
    applied: list[str] = []
    for name, provider in registered_providers():
        contribution = provider(session, context)
        if contribution is None or contribution.is_empty:
            continue
        applied.append(name)
        merged.runtime_profiles.extend(contribution.runtime_profiles)
        merged.validation_outcomes.extend(contribution.validation_outcomes)
        merged.benchmark_results.extend(contribution.benchmark_results)
        merged.limitations.extend(contribution.limitations)
    return merged, applied


def build_audit_report(
    session: Session, audit_id: str, settings: Settings | None = None
) -> AuditReport:
    settings = settings or get_settings()
    data = _AuditData(session, audit_id)
    contributions, applied = collect_contributions(session, data)
    files = _files_section(data)
    architecture = _architecture_section(data)
    findings_by_id = {f.id: f for f in data.findings}
    category_counts = Counter(f.category for f in data.findings)
    benchmarks = (
        BenchmarkSection(
            results=contributions.benchmark_results,
            chart_svg=benchmark_medians_svg(contributions.benchmark_results),
        )
        if contributions.benchmark_results
        else None
    )
    return AuditReport(
        metadata=ReportMetadata(
            title=f"Performance audit — {data.repository.name}",
            audit_id=data.audit.id,
            generated_at=datetime.now(UTC),
            generator=f"PerfPilot AI {__version__}",
            providers_applied=applied,
        ),
        executive_summary=_executive_summary(data, files, contributions),
        repository=_repository_section(data),
        files=files,
        architecture=architecture,
        findings=FindingsSection(
            total=len(data.findings),
            by_severity=_severity_counts(data.findings),
            by_category={c: category_counts[c] for c in FindingCategory if category_counts.get(c)},
            entries=_finding_entries(data.findings),
        ),
        evidence=_evidence_section(data),
        runtime=RuntimeSection(profiles=contributions.runtime_profiles)
        if contributions.runtime_profiles
        else None,
        complexity=_complexity_section(data.findings),
        strategies=_strategy_section(data.recommendations),
        code_changes=_code_changes_section(data.recommendations),
        validation=ValidationSection(outcomes=contributions.validation_outcomes)
        if contributions.validation_outcomes
        else None,
        benchmarks=benchmarks,
        effort=_effort_section(data.recommendations),
        roadmap=build_roadmap(data.recommendations, findings_by_id),
        limitations=_limitations_section(data, files, architecture, contributions, settings),
    )
