"""Priority scoring for static findings.

``priority_score = 100 * severity.weight * confidence.weight * path_criticality``

``path_criticality = reachability * path_role * loop_factor * fan_in_factor`` where

* ``reachability`` — 1.0 if the enclosing function is itself an entry point (route, task, CLI
  command, called from ``__main__``); 0.9 if the call graph reaches it from an entry-point function;
  0.75 if its module is reachable from an entry-point module through the import graph; 0.5
  otherwise; 0.3 for test code (always, regardless of the above).
* ``path_role`` — execution role of the enclosing code (``backend.audit.path_ranking``): request
  handler 1.0, request path or background task 0.9, library imported by request code 0.75, CLI /
  scripts 0.5 (1.0 when the repository has no handlers or tasks), internal 0.5, seed / fixtures /
  migrations 0.35, examples / docs 0.3, tests 0.2.
* ``loop_factor = min(1.0, 0.6 + 0.2 * (loop_depth + path_loop))`` — 0.6 outside loops, 0.8 in one
  loop, 1.0 at depth ≥ 2; ``path_loop`` is 1 when a call on the execution path runs inside a loop
  of its caller (a hot loop upstream), else 0.
* ``fan_in_factor = 0.7 + 0.3 * fan_in / max_fan_in`` — modules imported by many others weigh more
  (0.7 when no module has importers).

All factors are stored in ``Finding.priority_factors`` so the score can be audited; the role and
the execution path are stored in ``Finding.code_role`` and ``Finding.execution_path``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import networkx as nx

from backend.audit.path_ranking import PathRanker
from backend.audit.rule_engine import RuleFinding
from backend.indexing.call_graph import symbol_id
from backend.indexing.code_index import is_test_path
from backend.indexing.import_graph import reachable_modules
from backend.indexing.source import ParsedModule
from backend.models.audit_schemas import CodeIndex, Finding

REACH_ENTRY_SYMBOL = 1.0
REACH_CALLED_FROM_ENTRY = 0.9
REACH_MODULE_REACHABLE = 0.75
REACH_UNREACHED = 0.5
REACH_TEST_CODE = 0.3


def loop_factor(depth: int) -> float:
    return min(1.0, 0.6 + 0.2 * max(0, depth))


def fan_in_factor(fan_in: int, max_fan_in: int) -> float:
    return 0.7 + 0.3 * (fan_in / max_fan_in if max_fan_in > 0 else 0.0)


@dataclass
class PriorityContext:
    entry_symbols: set[str] = field(default_factory=set)
    called_from_entry: set[str] = field(default_factory=set)
    reachable: set[str] = field(default_factory=set)
    test_modules: set[str] = field(default_factory=set)
    fan_in: dict[str, int] = field(default_factory=dict)
    max_fan_in: int = 0
    ranker: PathRanker | None = None

    @classmethod
    def build(
        cls,
        index: CodeIndex,
        import_graph: nx.DiGraph,
        call_graph: nx.DiGraph,
        parsed: Mapping[str, ParsedModule] | None = None,
    ) -> PriorityContext:
        test_modules = {m.module_name for m in index.modules if is_test_path(m.file_path)}
        entry_symbols = {
            symbol_id(m.module_name, s.qualname)
            for m in index.modules
            if m.module_name not in test_modules
            for s in m.symbols
            if s.is_entrypoint
        }
        called: set[str] = set()
        for entry in entry_symbols:
            if entry in call_graph:
                called |= nx.descendants(call_graph, entry)
        entry_modules = [
            m.module_name
            for m in index.modules
            if m.is_entrypoint and m.module_name not in test_modules
        ]
        fan_in = {node.module_name: node.fan_in for node in index.graph.nodes}
        return cls(
            entry_symbols=entry_symbols,
            called_from_entry=called,
            reachable=reachable_modules(import_graph, entry_modules),
            test_modules=test_modules,
            fan_in=fan_in,
            max_fan_in=max(fan_in.values(), default=0),
            ranker=PathRanker(index, import_graph, call_graph, parsed),
        )

    def reachability(self, module_name: str, symbol: str | None) -> float:
        if module_name in self.test_modules:
            return REACH_TEST_CODE
        sid = symbol_id(module_name, symbol) if symbol else None
        if sid and self._matches(sid, self.entry_symbols):
            return REACH_ENTRY_SYMBOL
        if sid and self._matches(sid, self.called_from_entry):
            return REACH_CALLED_FROM_ENTRY
        if module_name in self.reachable:
            return REACH_MODULE_REACHABLE
        return REACH_UNREACHED

    @staticmethod
    def _matches(sid: str, symbols: set[str]) -> bool:
        """A finding in ``outer.inner`` counts for ``outer`` too (nested helpers, lambdas)."""
        module, _, qualname = sid.partition(":")
        parts = qualname.split(".")
        return any(
            symbol_id(module, ".".join(parts[:i])) in symbols for i in range(len(parts), 0, -1)
        )


def score(item: RuleFinding, ctx: PriorityContext) -> Finding:
    finding = item.finding
    reach = ctx.reachability(item.module_name, finding.location.symbol)
    role_factor = 1.0
    path_loop = 0
    if ctx.ranker is not None:
        assessment = ctx.ranker.assess(
            item.module_name, finding.location.file_path, finding.location.symbol
        )
        role_factor = assessment.factor
        path_loop = int(assessment.path_in_loop)
        finding.code_role = assessment.role
        finding.execution_path = assessment.path
    loops = loop_factor(item.loop_depth + path_loop)
    fan_in = ctx.fan_in.get(item.module_name, 0)
    fan = fan_in_factor(fan_in, ctx.max_fan_in)
    criticality = reach * role_factor * loops * fan
    severity_weight = finding.severity.weight
    confidence_weight = finding.confidence.weight
    finding.priority_score = round(100 * severity_weight * confidence_weight * criticality, 2)
    finding.priority_factors = {
        "severity_weight": severity_weight,
        "confidence_weight": confidence_weight,
        "entrypoint_reachability": reach,
        "path_role": role_factor,
        "loop_depth": float(item.loop_depth),
        "path_loop": float(path_loop),
        "loop_factor": round(loops, 4),
        "fan_in": float(fan_in),
        "fan_in_factor": round(fan, 4),
        "path_criticality": round(criticality, 4),
    }
    return finding


def prioritize(findings: list[RuleFinding], ctx: PriorityContext) -> list[Finding]:
    scored = [score(item, ctx) for item in findings]
    return sorted(
        scored,
        key=lambda f: (-f.priority_score, f.location.file_path, f.location.line_start, f.rule_id),
    )
