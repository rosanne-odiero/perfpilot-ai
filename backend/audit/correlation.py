"""Correlate measured runtime hotspots with indexed symbols and static findings.

A hotspot (``file`` relative to the repository, ``line`` = the code object's first line, function
name) is mapped to a :class:`SymbolInfo` when the file, first line and name match exactly
(``SymbolInfo.line_start`` includes decorators, like ``co_firstlineno``). Comprehension, lambda
and generator-expression code objects (``<genexpr>`` …) are mapped to the innermost enclosing
function. ``<module>`` code (import time) is matched only to module-level findings, i.e. findings
outside any indexed symbol, and is never reported as a runtime-only hotspot.

A static finding matches a hotspot when the innermost indexed symbol that contains the finding's
first line is the hotspot's symbol. The finding keeps its own provenance (``STATIC``); a
``MeasuredEvidence`` block (always ``MEASURED``) is attached and the priority is re-ranked::

    attributed_share = attributed_time(symbol) / Σ tottime(all functions)        (0..1)
    measured_factor  = 1 + MEASURED_WEIGHT * attributed_share                    (1..3)
    priority_score   = static_priority_score * measured_factor

``attributed_time(symbol)`` sums the attributed time (self time plus directly called non-repository
code, see ``backend/runtime/profile_stats.py``) of every hotspot mapped to the symbol, capped at the
function's cumulative time. Findings in functions that did not run keep their static score and get
no evidence. Repository functions with at least ``RUNTIME_ONLY_MIN_PERCENT`` attributed time and no
static finding are reported separately as *runtime-only hotspots*.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, Field

from backend.indexing.code_index import is_test_path
from backend.models.audit_schemas import CodeIndex, Finding, MeasuredEvidence, SymbolInfo

MEASURED_WEIGHT = 2.0
RUNTIME_ONLY_MIN_PERCENT = 1.0
MAX_RUNTIME_ONLY = 25
STATIC_SCORE_KEY = "static_priority_score"
MEASURED_FACTOR_KEYS = (STATIC_SCORE_KEY, "measured_factor", "measured_attributed_share")
MODULE_KEY = "<module>"
_FUNCTION_KINDS = {"function", "async_function", "method", "async_method"}


class CorrelationHotspot(BaseModel):
    id: str
    function_name: str
    file_path: str
    line_number: int
    total_calls: int
    internal_time_ms: float
    cumulative_time_ms: float
    percentage_of_profiled_time: float
    attributed_time_ms: float
    attributed_percentage: float
    is_user_code: bool


class SymbolMatch(BaseModel):
    symbol: str | None = Field(description="Qualified name; None for module-level code")
    file_path: str
    line_start: int
    line_end: int
    match: str = Field(description="'exact', 'enclosing' or 'module'")


class FindingCorrelation(BaseModel):
    finding_id: str
    rule_id: str
    hotspot_id: str
    location: SymbolMatch
    attributed_time_ms: float
    attributed_share: float
    measured_factor: float
    static_priority_score: float
    measured_priority_score: float


class RuntimeOnlyHotspot(BaseModel):
    hotspot_id: str
    function_name: str
    location: SymbolMatch
    internal_time_ms: float
    cumulative_time_ms: float
    attributed_time_ms: float
    attributed_percentage: float
    call_count: int
    is_test_code: bool


class CorrelationResult(BaseModel):
    run_id: str
    findings: list[Finding]
    correlations: list[FindingCorrelation]
    runtime_only: list[RuntimeOnlyHotspot]
    unmapped_hotspots: int = Field(description="Repository hotspots not matched to any symbol")


def measured_factor(attributed_share: float) -> float:
    return 1.0 + MEASURED_WEIGHT * max(0.0, min(1.0, attributed_share))


def static_score(finding: Finding) -> float:
    return finding.priority_factors.get(STATIC_SCORE_KEY, finding.priority_score)


def reset_to_static(finding: Finding) -> Finding:
    """Drop measured evidence and restore the static priority (idempotent)."""
    score = static_score(finding)
    factors = {k: v for k, v in finding.priority_factors.items() if k not in MEASURED_FACTOR_KEYS}
    return finding.model_copy(
        update={"measured": None, "priority_score": score, "priority_factors": factors}
    )


class _SymbolTable:
    def __init__(self, index: CodeIndex) -> None:
        self.by_file: dict[str, list[SymbolInfo]] = defaultdict(list)
        for module in index.modules:
            self.by_file[module.file_path].extend(module.symbols)

    def innermost(
        self, file_path: str, line: int, *, functions_only: bool = False
    ) -> SymbolInfo | None:
        best: SymbolInfo | None = None
        for symbol in self.by_file.get(file_path, ()):
            if functions_only and symbol.kind not in _FUNCTION_KINDS:
                continue
            if symbol.line_start <= line <= symbol.line_end and (
                best is None
                or symbol.line_end - symbol.line_start < best.line_end - best.line_start
            ):
                best = symbol
        return best

    def resolve(self, hotspot: CorrelationHotspot) -> SymbolMatch | None:
        file_path, line, name = hotspot.file_path, hotspot.line_number, hotspot.function_name
        if file_path not in self.by_file:
            return None
        if name == "<module>":
            return SymbolMatch(
                symbol=None,
                file_path=file_path,
                line_start=max(1, line),
                line_end=max(1, line),
                match="module",
            )
        exact = [
            s
            for s in self.by_file[file_path]
            if s.line_start == line and s.qualname.rsplit(".", 1)[-1] == name
        ]
        if exact:
            symbol = min(exact, key=lambda s: s.line_end - s.line_start)
            return _symbol_match(symbol, "exact")
        if name.startswith("<"):
            enclosing = self.innermost(file_path, line, functions_only=True)
            if enclosing is not None:
                return _symbol_match(enclosing, "enclosing")
        return None


def _symbol_match(symbol: SymbolInfo, match: str) -> SymbolMatch:
    return SymbolMatch(
        symbol=symbol.qualname,
        file_path=symbol.file_path,
        line_start=symbol.line_start,
        line_end=symbol.line_end,
        match=match,
    )


def correlate(
    run_id: str,
    index: CodeIndex,
    findings: Sequence[Finding],
    hotspots: Sequence[CorrelationHotspot],
) -> CorrelationResult:
    """Pure correlation; returns re-ranked findings (sorted by priority) and the links."""
    table = _SymbolTable(index)
    groups: dict[tuple[str, str], list[tuple[CorrelationHotspot, SymbolMatch]]] = defaultdict(list)
    unmapped: list[CorrelationHotspot] = []
    for hotspot in hotspots:
        if not hotspot.is_user_code:
            continue
        match = table.resolve(hotspot)
        if match is None:
            unmapped.append(hotspot)
        elif match.symbol is None:
            groups[(match.file_path, MODULE_KEY)].append((hotspot, match))
        else:
            groups[(match.file_path, match.symbol)].append((hotspot, match))

    findings_by_symbol: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        symbol = table.innermost(finding.location.file_path, finding.location.line_start)
        name = symbol.qualname if symbol is not None else MODULE_KEY
        findings_by_symbol[(finding.location.file_path, name)].append(finding)

    updated = {f.id: reset_to_static(f) for f in findings}
    correlations: list[FindingCorrelation] = []
    runtime_only: list[RuntimeOnlyHotspot] = []
    for key, members in groups.items():
        primary, location = max(
            members, key=lambda item: (item[1].match == "exact", item[0].cumulative_time_ms)
        )
        attributed_ms = sum(h.attributed_time_ms for h, _ in members)
        attributed_pct = sum(h.attributed_percentage for h, _ in members)
        if location.match == "exact" and primary.cumulative_time_ms > 0:
            scale = min(1.0, primary.cumulative_time_ms / attributed_ms) if attributed_ms else 1.0
            attributed_ms *= scale
            attributed_pct *= scale
        share = attributed_pct / 100.0
        matched = findings_by_symbol.get(key, [])
        if not matched:
            if location.match != "module" and attributed_pct >= RUNTIME_ONLY_MIN_PERCENT:
                runtime_only.append(
                    RuntimeOnlyHotspot(
                        hotspot_id=primary.id,
                        function_name=location.symbol or primary.function_name,
                        location=location,
                        internal_time_ms=primary.internal_time_ms,
                        cumulative_time_ms=primary.cumulative_time_ms,
                        attributed_time_ms=attributed_ms,
                        attributed_percentage=attributed_pct,
                        call_count=primary.total_calls,
                        is_test_code=is_test_path(location.file_path),
                    )
                )
            continue
        factor = measured_factor(share)
        for finding in matched:
            base = updated[finding.id]
            static = base.priority_score
            score = round(static * factor, 2)
            evidence = MeasuredEvidence(
                profile_id=run_id,
                function_name=location.symbol or primary.function_name,
                internal_time_ms=primary.internal_time_ms,
                cumulative_time_ms=primary.cumulative_time_ms,
                call_count=primary.total_calls,
                percentage_of_profiled_time=primary.percentage_of_profiled_time,
                attributed_time_ms=attributed_ms,
                attributed_percentage=attributed_pct,
                measured_factor=round(factor, 4),
                hotspot_file_path=primary.file_path,
                hotspot_line=primary.line_number,
                note=(
                    f"{attributed_pct:.1f}% of profiled time attributed to this function "
                    "(self time plus direct library calls)."
                ),
            )
            updated[finding.id] = base.model_copy(
                update={
                    "measured": evidence,
                    "priority_score": score,
                    "priority_factors": {
                        **base.priority_factors,
                        STATIC_SCORE_KEY: static,
                        "measured_factor": round(factor, 4),
                        "measured_attributed_share": round(share, 6),
                    },
                }
            )
            correlations.append(
                FindingCorrelation(
                    finding_id=finding.id,
                    rule_id=finding.rule_id,
                    hotspot_id=primary.id,
                    location=location,
                    attributed_time_ms=attributed_ms,
                    attributed_share=share,
                    measured_factor=factor,
                    static_priority_score=static,
                    measured_priority_score=score,
                )
            )

    ranked = sorted(
        updated.values(),
        key=lambda f: (-f.priority_score, f.location.file_path, f.location.line_start, f.rule_id),
    )
    correlations.sort(key=lambda c: (-c.measured_priority_score, c.finding_id))
    runtime_only.sort(
        key=lambda r: (-r.attributed_time_ms, r.location.file_path, r.location.line_start)
    )
    return CorrelationResult(
        run_id=run_id,
        findings=ranked,
        correlations=correlations,
        runtime_only=runtime_only[:MAX_RUNTIME_ONLY],
        unmapped_hotspots=len(unmapped),
    )
