"""Allocation-heavy patterns: deep copies, constant literals and DataFrame growth in loops."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.audit.rules.loops import grows_in_loop, names_in, short
from backend.indexing.source import ParsedModule
from backend.models.enums import Confidence, FindingCategory, Severity

LARGE_LITERAL = 8


class DeepcopyInLoop(BaseRule):
    meta = RuleMeta("PP-ALLOC-001", "copy.deepcopy inside a loop", FindingCategory.ALLOCATIONS)
    loop_based = True
    suggestion = (
        "Copy only what is mutated (a shallow `dict(x)`/`list(x)`/`copy.copy`, "
        "`dataclasses.replace`), build new objects instead of mutating copies, or copy once "
        "outside the loop if the source does not change."
    )
    tradeoffs = (
        "Shallow copies share nested objects, so mutating nested data would affect the original; "
        "only safe after checking which parts are modified."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        loop = scope.innermost_loop
        if loop is None or scope.qualify(node.func) != "copy.deepcopy" or not node.args:
            return
        if names_in(node.args[0]) & loop.assigned:
            return  # each iteration copies a different (per-item) object: inherent work
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` deep-copies the same outer object on every iteration of the "
                f"loop at line {loop.line}. `deepcopy` walks and duplicates the entire object "
                "graph (with memo bookkeeping), so each iteration pays for the whole structure, "
                "which grows if the loop adds to it."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.MEDIUM,
        )


class ConstantLiteralInLoop(BaseRule):
    meta = RuleMeta(
        "PP-ALLOC-002", "Large constant literal rebuilt inside a loop", FindingCategory.ALLOCATIONS
    )
    loop_based = True
    suggestion = (
        "Define the literal once outside the loop (a module-level constant; use a tuple or "
        "frozenset if it is never modified)."
    )
    tradeoffs = (
        "A shared constant must not be mutated by the loop body; if it is, keep the per-iteration "
        "copy (or copy the constant explicitly)."
    )

    def visit_List(self, node: ast.List, scope: ScopeTracker) -> None:
        self._check(node, node.elts, scope)

    def visit_Set(self, node: ast.Set, scope: ScopeTracker) -> None:
        self._check(node, node.elts, scope)

    def visit_Dict(self, node: ast.Dict, scope: ScopeTracker) -> None:
        if all(k is not None for k in node.keys):
            elements = [k for k in node.keys if k is not None] + list(node.values)
            self._check(node, elements, scope, size=len(node.keys))

    def _check(
        self, node: ast.expr, elements: list[ast.expr], scope: ScopeTracker, size: int = 0
    ) -> None:
        loop = scope.innermost_loop
        size = size or len(elements)
        if loop is None or size < LARGE_LITERAL:
            return
        if not all(isinstance(e, ast.Constant) for e in elements):
            return
        parent = scope.parent
        if isinstance(parent, ast.Compare) or scope.is_loop_iterable(node):
            return  # folded into a constant by the compiler or evaluated once
        if isinstance(parent, ast.List | ast.Set | ast.Dict | ast.Tuple):
            return  # report the outermost literal only
        if isinstance(parent, ast.Assign):
            names = {t.id for t in parent.targets if isinstance(t, ast.Name)}
            if names & loop.mutated or any(grows_in_loop(n, loop) for n in names):
                return  # a fresh mutable container per iteration is intended
        self.emit(
            scope,
            node,
            description=(
                f"A {size}-element constant {type(node).__name__.lower()} literal is rebuilt on "
                f"every iteration of the loop at line {loop.line}; Python allocates and fills a "
                "new container each time even though the contents never change."
            ),
            severity=Severity.LOW,
            confidence=Confidence.MEDIUM,
        )


class DataFrameGrowthInLoop(BaseRule):
    meta = RuleMeta(
        "PP-ALLOC-003", "DataFrame grown row by row inside a loop", FindingCategory.ALLOCATIONS
    )
    loop_based = True
    suggestion = (
        "Collect rows (dicts/tuples) or partial frames in a Python list and build the DataFrame "
        "once after the loop with `pd.DataFrame(rows)` or a single `pd.concat(frames)`."
    )
    tradeoffs = (
        "Peak memory holds the list plus the final frame; dtype inference from records can differ "
        "from incremental concatenation (check dtypes and index)."
    )

    def begin_module(self, module: ParsedModule) -> None:
        self._uses_pandas = module.aliases.imports_module("pandas")

    def visit_Assign(self, node: ast.Assign, scope: ScopeTracker) -> None:
        if not self._uses_pandas or not scope.in_loop or not isinstance(node.value, ast.Call):
            return
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        call = node.value
        qualified = scope.qualify(call.func) or ""
        grows = False
        if qualified == "pandas.concat" and call.args:
            grows = bool(targets & names_in(call.args[0]))
        elif isinstance(call.func, ast.Attribute) and call.func.attr in {"append", "_append"}:
            grows = isinstance(call.func.value, ast.Name) and call.func.value.id in targets
        if not grows:
            return
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` rebuilds the DataFrame on every iteration of the loop at line "
                f"{scope.loops[-1].line}. Each concat/append copies all rows accumulated so far "
                "into a new frame."
            ),
            severity=Severity.HIGH,
            confidence=Confidence.HIGH,
            complexity_note="estimate: O(n²) row copies for n iterations vs O(n) when built once.",
        )


class IterrowsLoop(BaseRule):
    meta = RuleMeta(
        "PP-ALLOC-004", "Row-wise DataFrame iteration with iterrows()", FindingCategory.ALLOCATIONS
    )
    suggestion = (
        "Prefer vectorized column operations; if a Python loop is required, use "
        "`df.itertuples(index=False)` or iterate over `df.to_dict('records')`."
    )
    tradeoffs = (
        "Vectorized code can be harder to read; `itertuples` yields namedtuples (column names must "
        "be valid identifiers); `iterrows` upcasts mixed dtypes to a common dtype per row, so "
        "switching can change value types seen by the loop body."
    )

    def begin_module(self, module: ParsedModule) -> None:
        self._uses_pandas = module.aliases.imports_module("pandas")

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if (
            self._uses_pandas
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "iterrows"
            and not node.args
            and scope.is_loop_iterable(node)
        ):
            self.emit(
                scope,
                node,
                description=(
                    f"`{short(node)}` builds a pandas Series object for every row. The per-row "
                    "boxing and dtype handling usually dominate the loop's cost compared with "
                    "vectorized operations or `itertuples()`."
                ),
                severity=Severity.MEDIUM,
                confidence=Confidence.MEDIUM,
            )
