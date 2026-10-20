"""Collection rules: linear list operations in loops, lists as queues, needless copies."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.audit.rules.kinds import Kind
from backend.audit.rules.loops import short
from backend.models.enums import Confidence, FindingCategory, Severity

_SEARCH_METHODS = {
    "index": (
        "`list.index` scans from the start until it finds the value",
        "Build a dict from value to position once (`pos = {v: i for i, v in enumerate(items)}`) "
        "and look positions up in O(1).",
        "Values must be hashable; for duplicates decide whether the first or last position wins "
        "(`list.index` returns the first); the dict must be updated if the list changes.",
    ),
    "count": (
        "`list.count` scans the whole list on every call",
        "Count all values once with `collections.Counter(items)` and read counts from it.",
        "Values must be hashable; the Counter is a snapshot and must be updated if the list "
        "changes; it uses extra memory proportional to the number of distinct values.",
    ),
    "remove": (
        "`list.remove` scans for the value and then shifts every later element",
        "Build the result in one pass (`[x for x in items if x not in to_drop]` with `to_drop` a "
        "set) or keep the data in a set/dict when order is not needed.",
        "Rebuilding creates a new list (other references to the old list keep the old contents); "
        "a set loses ordering and duplicates; `remove` only drops the first occurrence.",
    ),
}


class ListSearchInLoop(BaseRule):
    meta = RuleMeta(
        "PP-COLL-001", "Linear list search/removal inside a loop", FindingCategory.COLLECTIONS
    )
    loop_based = True

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if not scope.in_loop or not isinstance(node.func, ast.Attribute):
            return
        method = node.func.attr
        if method not in _SEARCH_METHODS or len(node.args) != 1:
            return
        receiver = node.func.value
        if scope.kind(receiver) is not Kind.LIST:
            return
        mechanism, suggestion, tradeoffs = _SEARCH_METHODS[method]
        is_param = isinstance(receiver, ast.Name) and scope.is_param(receiver.id)
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` runs inside a loop on a list; {mechanism}, so the cost of each "
                "call grows with the list length and the loop can become quadratic."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.MEDIUM if is_param else Confidence.HIGH,
            complexity_note=(
                "estimate: O(n·m) for n iterations over a list of m items; a dict/Counter/set "
                "lookup is O(1) on average."
            ),
            suggestion=suggestion,
            tradeoffs=tradeoffs,
        )


class ListAsQueue(BaseRule):
    meta = RuleMeta(
        "PP-COLL-002",
        "List used as a queue (pop(0)/insert(0, …)) in a loop",
        FindingCategory.COLLECTIONS,
    )
    loop_based = True
    suggestion = (
        "Use `collections.deque` and `popleft()` / `appendleft()`, which are O(1) at both ends."
    )
    tradeoffs = (
        "A deque does not support slicing and its indexing in the middle is O(n); code that "
        "relies on list-only methods (sort, slicing) must change; ordering semantics are the same."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if not scope.in_loop or not isinstance(node.func, ast.Attribute):
            return
        method = node.func.attr
        first = node.args[0] if node.args else None
        is_front = isinstance(first, ast.Constant) and first.value == 0
        if not is_front or (method, len(node.args)) not in {("pop", 1), ("insert", 2)}:
            return
        if scope.kind(node.func.value) is not Kind.LIST:
            return
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` removes/inserts at the front of a list inside a loop. Lists are "
                "contiguous arrays, so every front operation shifts all remaining elements."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.HIGH,
            complexity_note=(
                "estimate: O(n) per front operation, O(n²) over n iterations; deque makes it O(n)."
            ),
        )


class NeedlessMaterialization(BaseRule):
    meta = RuleMeta(
        "PP-COLL-003",
        "Needless list materialization inside a loop",
        FindingCategory.ALLOCATIONS,
    )
    loop_based = True
    suggestion = (
        "Use the view or iterator directly: `for k in d`, `k in d`, `len(d)`, or "
        "`sum(1 for _ in it)` to count an iterator."
    )
    tradeoffs = (
        "Copying `d.keys()` is required when the dict is modified while iterating; consuming an "
        "iterator to count it still exhausts it."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if not scope.in_loop:
            return
        qualified = scope.qualify(node.func)
        if qualified == "len" and len(node.args) == 1:
            inner = node.args[0]
            if isinstance(inner, ast.Call) and scope.qualify(inner.func) == "list" and inner.args:
                self._report(
                    scope,
                    node,
                    "builds a full list only to take its length",
                )
            return
        if qualified != "list" or len(node.args) != 1:
            return
        inner = node.args[0]
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "keys"
            and not inner.args
        ):
            return
        parent = scope.parent
        if isinstance(parent, ast.Compare):
            return  # membership in a copied list is reported by PP-LOOP-001
        if scope.is_loop_iterable(node):
            return  # defensive copy so the loop body may modify the dict
        self._report(scope, node, "copies the dict's keys into a new list")

    def _report(self, scope: ScopeTracker, node: ast.Call, what: str) -> None:
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` {what} on every iteration of the loop at line "
                f"{scope.loops[-1].line}; the temporary list is allocated and discarded each time."
            ),
            severity=Severity.LOW,
            confidence=Confidence.HIGH,
        )
