"""Deterministic advisor: static findings + code index -> recommendations and insights.

This is the default path (no API key, no network). Every output is derived from rule findings,
so its provenance is ``STATIC``. Findings are grouped so one recommendation covers one change:

* the same rule in the same function (``group_by_module`` rules — N+1 queries, blocking calls in
  coroutines, HTTP clients — are grouped per module instead, e.g. one N+1 recommendation per
  router);
* cross-module patterns become :class:`ArchitectureInsight` s (shared HTTP client, async/sync
  boundary, systemic N+1, high fan-in hot modules, findings outside request paths).

Effort comes from the rule's base effort, raised one level for >= 3 functions and one more for
>= 3 files. Where a safe mechanical rewrite exists (``backend.ai.rewrites``) a unified diff
generated from the real file lines is attached, after checking that it applies cleanly and that
the result still parses.
"""

from __future__ import annotations

import ast
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backend.ai.advice_catalog import RuleAdvice, advice_for
from backend.ai.diffs import DiffApplyError, apply_edits, apply_unified_diff, make_unified_diff
from backend.ai.rewrites import REWRITES, SourceFile
from backend.models.audit_schemas import (
    ArchitectureInsight,
    CodeIndex,
    Finding,
    Recommendation,
)
from backend.models.enums import CodeRole, EffortEstimate, Provenance

MAX_CITATIONS = 12
MAX_LISTED_SUGGESTIONS = 3
MAX_HOT_MODULES = 3
SECONDARY_WEIGHT = 0.1
SYNC_DB_MARKERS = ("synchronous database",)
NON_REQUEST_ROLES = frozenset(
    {CodeRole.SEED_MIGRATION, CodeRole.TEST, CodeRole.EXAMPLE_DOCS, CodeRole.CLI_SCRIPT}
)
ROLE_LABELS: dict[CodeRole, str] = {
    CodeRole.REQUEST_HANDLER: "a request handler",
    CodeRole.REQUEST_PATH: "code called from a request handler",
    CodeRole.BACKGROUND_TASK: "a background task (or code it calls)",
    CodeRole.LIBRARY: "library code imported by request-handling modules",
    CodeRole.INTERNAL: "internal code with no known path from an entry point",
    CodeRole.CLI_SCRIPT: "a CLI command or script",
    CodeRole.SEED_MIGRATION: "seed, fixture or migration code",
    CodeRole.EXAMPLE_DOCS: "example or documentation code",
    CodeRole.TEST: "test code",
}
_EFFORT_ORDER = [EffortEstimate.SMALL, EffortEstimate.MEDIUM, EffortEstimate.LARGE]


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]
    return f"{prefix}-{digest}"


def first_sentence(text: str) -> str:
    sentence = text.strip().split(". ", 1)[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


@dataclass
class FindingGroup:
    """Findings that one recommendation addresses (one rule, one function or one module)."""

    rule_id: str
    file_path: str
    symbol: str | None
    findings: list[Finding]
    advice: RuleAdvice
    variant: str = ""

    @property
    def key(self) -> str:
        return f"{self.rule_id}|{self.variant}|{self.file_path}|{self.symbol or ''}"

    @property
    def score(self) -> float:
        scores = sorted((f.priority_score for f in self.findings), reverse=True)
        return round(scores[0] + SECONDARY_WEIGHT * sum(scores[1:]), 4)

    @property
    def symbols(self) -> list[str]:
        return _dedupe(f.location.symbol or "<module>" for f in self.findings)

    @property
    def top(self) -> Finding:
        return self.findings[0]


@dataclass
class AdvisorOutput:
    recommendations: list[Recommendation] = field(default_factory=list)
    insights: list[ArchitectureInsight] = field(default_factory=list)
    groups: list[FindingGroup] = field(default_factory=list)
    diffs_generated: int = 0
    diffs_rejected: int = 0


def _variant(finding: Finding) -> str:
    if finding.rule_id == "PP-ASYNC-001" and any(
        marker in finding.description for marker in SYNC_DB_MARKERS
    ):
        return "sync-db"
    return ""


def group_findings(findings: Iterable[Finding]) -> list[FindingGroup]:
    buckets: dict[tuple[str, str, str, str | None], list[Finding]] = defaultdict(list)
    for finding in findings:
        advice = advice_for(finding.rule_id)
        symbol = None if advice.group_by_module else finding.location.symbol
        key = (finding.rule_id, _variant(finding), finding.location.file_path, symbol)
        buckets[key].append(finding)
    groups = [
        FindingGroup(
            rule_id=rule_id,
            file_path=path,
            symbol=symbol,
            variant=variant,
            advice=advice_for(rule_id),
            findings=sorted(
                items,
                key=lambda f: (-f.priority_score, f.location.line_start, f.id),
            ),
        )
        for (rule_id, variant, path, symbol), items in buckets.items()
    ]
    return sorted(groups, key=lambda g: (-g.score, g.file_path, g.top.location.line_start))


def estimate_effort(base: EffortEstimate, functions: int, files: int) -> EffortEstimate:
    level = _EFFORT_ORDER.index(base) + (functions >= 3) + (files >= 3)
    return _EFFORT_ORDER[min(level, len(_EFFORT_ORDER) - 1)]


class DeterministicAdvisor:
    """Turns prioritized findings into recommendations and insights without any model."""

    def __init__(
        self,
        index: CodeIndex,
        sources: Mapping[str, str],
        root: Path | None = None,
    ) -> None:
        self.index = index
        self.sources = sources
        self.root = root.resolve() if root else None
        self.module_by_path = {m.file_path: m.module_name for m in index.modules}

    def advise(self, findings: list[Finding]) -> AdvisorOutput:
        output = AdvisorOutput(groups=group_findings(findings))
        for rank, group in enumerate(output.groups, start=1):
            output.recommendations.append(self._recommend(group, rank, output))
        output.insights = self._insights(findings)
        return output

    # ---------------------------------------------------------------- recommendations

    def _where(self, group: FindingGroup) -> str:
        symbols = [s for s in group.symbols if s != "<module>"]
        if group.symbol:
            return f"`{group.symbol}` ({group.file_path})"
        if len(symbols) == 1 and len(group.symbols) == 1:
            return f"`{symbols[0]}` ({group.file_path})"
        if not symbols:
            return f"module-level code of {group.file_path}"
        return f"{len(group.symbols)} functions in {group.file_path}"

    def _recommend(self, group: FindingGroup, rank: int, output: AdvisorOutput) -> Recommendation:
        advice = group.advice
        top = group.top
        where = self._where(group)
        count = len(group.findings)
        noun = "location" if count == 1 else "locations"
        diff, covered = self._diff(group, output)
        strategy = [advice.strategy]
        if group.variant == "sync-db":
            strategy = [
                "Pick one async/sync boundary for these endpoints: declare them as plain `def` "
                "(FastAPI runs those in a thread pool) or move the data access to an async "
                "driver / `AsyncSession`; avoid calling a synchronous session from `async def`."
            ]
        strategy.extend(
            f"- {s}" for s in _dedupe(f.suggestion for f in group.findings)[:MAX_LISTED_SUGGESTIONS]
        )
        if diff:
            strategy.append(
                f"A mechanical diff is attached for {covered} of {count} {noun}; review it like "
                "any other change and run the tests."
            )
        title_where = group.symbol or (
            group.symbols[0] if len(group.symbols) == 1 else group.file_path.rsplit("/", 1)[-1]
        )
        if title_where == "<module>":
            title_where = group.file_path.rsplit("/", 1)[-1]
        return Recommendation(
            id=stable_id("rec", group.key, *sorted(f.id for f in group.findings)),
            title=f"{advice.title} in {title_where}",
            summary=(
                f"{count} {top.title.lower()} {noun} in {where}. {first_sentence(top.description)}"
            ),
            rationale=self._rationale(group),
            proposed_strategy="\n".join(strategy),
            finding_ids=[f.id for f in group.findings],
            citations=[f.location for f in group.findings[:MAX_CITATIONS]],
            provenance=Provenance.STATIC,
            effort=estimate_effort(
                advice.effort,
                functions=len(group.symbols),
                files=len({f.location.file_path for f in group.findings}),
            ),
            uncertainty=self._uncertainty(group),
            expected_effect=self._expected_effect(group),
            correctness_risks=" ".join(
                _dedupe([advice.risks, *(f.tradeoffs for f in group.findings[:2])])
            ),
            suggested_diff=diff,
            priority_rank=rank,
            generated_by="deterministic",
            kind=advice.kind if group.variant != "sync-db" else "async-db-boundary",
        )

    def _rationale(self, group: FindingGroup) -> str:
        parts = ["Static evidence (not measured):"]
        parts.extend(_dedupe(first_sentence(f.description) for f in group.findings)[:3])
        notes = _dedupe(f.complexity_note or "" for f in group.findings)
        if notes:
            parts.append(f"Complexity {notes[0]}")
        role = group.top.code_role
        if role is not None:
            parts.append(f"The top location is {ROLE_LABELS[role]}.")
            path = group.top.execution_path
            if len(path) > 1:
                parts.append(f"Execution path: {' -> '.join(path)}.")
        return " ".join(parts)

    def _uncertainty(self, group: FindingGroup) -> str:
        confidence = Counter(f.confidence.value for f in group.findings)
        listed = ", ".join(f"{level} x{n}" for level, n in sorted(confidence.items()))
        return (
            f"Derived from {len(group.findings)} static finding(s) (confidence: {listed}). Static "
            "analysis cannot see input sizes, call frequency or the database/network latency, so "
            "the real impact is unknown until the code is profiled before and after the change."
        )

    def _expected_effect(self, group: FindingGroup) -> str:
        effect = group.advice.expected_effect
        role = group.top.code_role
        if role in NON_REQUEST_ROLES:
            assert role is not None
            effect += (
                f" This code is {ROLE_LABELS[role]}, so the practical benefit is probably limited "
                "to that workflow."
            )
        return effect

    # ---------------------------------------------------------------- diffs

    def _diff(self, group: FindingGroup, output: AdvisorOutput) -> tuple[str | None, int]:
        rewrite = REWRITES.get(group.rule_id)
        text = self.sources.get(group.file_path)
        if rewrite is None or text is None or "\r" in text or not self._matches_disk(group, text):
            return None, 0
        try:
            source = SourceFile(group.file_path, text)
        except SyntaxError:
            return None, 0
        edits = []
        covered = 0
        for finding in sorted(group.findings, key=lambda f: f.location.line_start):
            result = rewrite(source, finding)
            if result:
                edits.extend(result)
                covered += 1
        if not edits:
            return None, 0
        try:
            new_text = apply_edits(text, edits)
            ast.parse(new_text)
            diff = make_unified_diff(group.file_path, text, new_text)
            if apply_unified_diff(text, diff) != new_text:
                raise DiffApplyError("round trip changed the result")
        except (ValueError, SyntaxError):
            output.diffs_rejected += 1
            return None, 0
        output.diffs_generated += 1
        return diff, covered

    def _matches_disk(self, group: FindingGroup, text: str) -> bool:
        """The diff targets the file on disk, so the indexed text must be byte-identical."""
        if self.root is None:
            return True
        path = (self.root / group.file_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return False
        try:
            return path.read_bytes().decode("utf-8") == text
        except (OSError, UnicodeDecodeError):
            return False

    # ---------------------------------------------------------------- insights

    def _insight(
        self, kind: str, title: str, description: str, findings: list[Finding]
    ) -> ArchitectureInsight:
        ordered = sorted(findings, key=lambda f: (-f.priority_score, f.location.file_path))
        modules = _dedupe(
            self.module_by_path.get(f.location.file_path, f.location.file_path) for f in ordered
        )
        return ArchitectureInsight(
            id=stable_id("ins", kind, *sorted(f.id for f in findings)),
            title=title,
            description=description,
            modules=modules,
            citations=[f.location for f in ordered[:MAX_CITATIONS]],
            provenance=Provenance.STATIC,
            generated_by="deterministic",
        )

    def _insights(self, findings: list[Finding]) -> list[ArchitectureInsight]:
        insights: list[ArchitectureInsight] = []
        request_side = [f for f in findings if f.code_role not in NON_REQUEST_ROLES]

        def files(items: list[Finding]) -> set[str]:
            return {f.location.file_path for f in items}

        clients = [f for f in request_side if f.rule_id == "PP-NET-002"]
        if len(files(clients)) >= 2:
            insights.append(
                self._insight(
                    "shared-http-client",
                    "Introduce a shared HTTP client",
                    f"{len(clients)} call sites in {len(files(clients))} modules create a new "
                    "HTTP client or session per call, so every request pays for connection and "
                    "TLS setup and nothing is pooled. A single client owned by the application "
                    "(created at startup or in a lifespan/dependency, closed on shutdown) and "
                    "injected where needed fixes this once for every module, and gives one place "
                    "for timeouts, retries and connection limits.",
                    clients,
                )
            )
        sync_db = [f for f in request_side if _variant(f) == "sync-db"]
        coroutines = {(f.location.file_path, f.location.symbol) for f in sync_db}
        if len(coroutines) >= 2:
            handlers = {
                (f.location.file_path, f.location.symbol)
                for f in sync_db
                if f.code_role is CodeRole.REQUEST_HANDLER
            }
            insights.append(
                self._insight(
                    "async-sync-boundary",
                    "Synchronous database access inside async code",
                    f"{len(coroutines)} coroutines ({len(handlers)} of them request handlers) in "
                    f"{len(files(sync_db))} modules use a synchronous database session or driver "
                    "directly. Each query blocks the event loop thread, so concurrent requests "
                    "wait behind it. This is an architectural boundary decision rather than a "
                    "local fix: either run these endpoints as plain `def` (thread pool) or move "
                    "the data layer to an async driver/`AsyncSession`, and apply it consistently.",
                    sync_db,
                )
            )
        blocking = [f for f in request_side if f.rule_id == "PP-ASYNC-001" and not _variant(f)]
        blocking_coroutines = {(f.location.file_path, f.location.symbol) for f in blocking}
        if len(blocking_coroutines) >= 3:
            insights.append(
                self._insight(
                    "event-loop-blocking",
                    "Blocking calls on the event loop across the codebase",
                    f"{len(blocking_coroutines)} coroutines in {len(files(blocking))} modules make "
                    "blocking calls (sleep, HTTP, subprocess or file I/O). A shared convention — "
                    "async clients for I/O and `asyncio.to_thread` for unavoidable blocking work — "
                    "prevents the pattern from reappearing in new endpoints.",
                    blocking,
                )
            )
        n_plus_one = [f for f in request_side if f.rule_id == "PP-DB-001"]
        if len(files(n_plus_one)) >= 2:
            insights.append(
                self._insight(
                    "systemic-n-plus-one",
                    "N+1 query pattern recurs across modules",
                    f"{len(n_plus_one)} loops in {len(files(n_plus_one))} modules issue one "
                    "query per item. Recurring N+1 patterns usually point at a data-access layer "
                    "without batch helpers; adding bulk-fetch functions (load by ID list, eager "
                    "loading options) to the repository/service layer makes the batched form the "
                    "easy default.",
                    n_plus_one,
                )
            )
        insights.extend(self._hot_modules(findings))
        deprioritized = [f for f in findings if f.code_role in NON_REQUEST_ROLES]
        if deprioritized:
            roles = Counter(f.code_role.value for f in deprioritized if f.code_role)
            listed = ", ".join(f"{role} x{n}" for role, n in sorted(roles.items()))
            insights.append(
                self._insight(
                    "deprioritized-findings",
                    "Findings outside request paths were deprioritized",
                    f"{len(deprioritized)} of {len(findings)} findings are in code that does not "
                    f"serve requests ({listed}). Their priority is scaled down by the path-role "
                    "factor; review them only if those workflows are slow in practice.",
                    deprioritized,
                )
            )
        return insights

    def _hot_modules(self, findings: list[Finding]) -> list[ArchitectureInsight]:
        nodes = [n for n in self.index.graph.nodes if n.fan_in > 0]
        if not nodes:
            return []
        fan_ins = sorted(n.fan_in for n in nodes)
        threshold = max(3, fan_ins[int(0.9 * (len(fan_ins) - 1))])
        by_file: dict[str, list[Finding]] = defaultdict(list)
        for finding in findings:
            by_file[finding.location.file_path].append(finding)
        insights = []
        for node in sorted(nodes, key=lambda n: (-n.fan_in, n.module_name)):
            module_findings = by_file.get(node.file_path, [])
            if node.fan_in >= threshold and len(module_findings) >= 2:
                insights.append(
                    self._insight(
                        f"hot-module:{node.module_name}",
                        f"High fan-in module with findings: {node.module_name}",
                        f"`{node.module_name}` is imported by {node.fan_in} modules and has "
                        f"{len(module_findings)} findings. Changes here affect many callers, "
                        "so fixes have broad reach, but they also need the broadest regression "
                        "testing; consider giving it focused benchmarks.",
                        module_findings,
                    )
                )
        return insights[:MAX_HOT_MODULES]
