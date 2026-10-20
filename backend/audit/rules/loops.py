"""Loop rules: membership scans, nested-loop joins, loop-invariant work, sorting in loops."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, LoopFrame, RuleMeta, ScopeTracker
from backend.audit.rules.kinds import Kind
from backend.indexing.ast_index import iter_own_nodes
from backend.models.enums import Confidence, FindingCategory, Severity

SMALL_LITERAL = 6
GROW_METHODS = frozenset({"append", "extend", "insert", "appendleft", "add", "update"})


def names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def short(node: ast.AST, limit: int = 60) -> str:
    text = ast.unparse(node)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def grows_in_loop(name: str, loop: LoopFrame) -> bool:
    """True when ``name`` is extended (append/extend/+=/…) somewhere inside the loop body."""
    for node in iter_own_nodes([loop.node]):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in GROW_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
        ):
            return True
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return True
    return False


def reassigned_in_loop(name: str, loop: LoopFrame) -> bool:
    """True when the loop body rebinds ``name`` with a plain assignment (fresh value per pass)."""
    body = loop.node.body if isinstance(loop.node, ast.For | ast.AsyncFor | ast.While) else []
    for node in iter_own_nodes(list(body)):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node.value)):
                continue  # x = x + ... style accumulation
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.For | ast.AsyncFor):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


class ListMembershipInLoop(BaseRule):
    meta = RuleMeta(
        "PP-LOOP-001", "List membership test inside a loop", FindingCategory.ALGORITHMIC
    )
    suggestion = (
        "Build a set (or dict) once before the loop, e.g. `allowed = set(items)`, and test "
        "`x in allowed`; when the list grows inside the loop, maintain a parallel set."
    )
    tradeoffs = (
        "Set elements must be hashable; a set loses ordering and duplicates (keep the list if you "
        "need them); building it costs O(m) time and extra memory, so it only pays off when the "
        "test runs many times."
    )

    def visit_Compare(self, node: ast.Compare, scope: ScopeTracker) -> None:
        if not scope.in_loop:
            return
        for op, container in zip(node.ops, node.comparators, strict=True):
            if not isinstance(op, ast.In | ast.NotIn):
                continue
            verdict = self._list_evidence(container, scope)
            if verdict is None:
                continue
            evidence, severity, confidence = verdict
            self.emit(
                scope,
                node,
                description=(
                    f"`{short(node)}` runs inside a loop and {evidence}. Membership tests on a "
                    "list compare elements one by one, so each test costs time proportional to "
                    "the list length and the loop as a whole can become quadratic as data grows."
                ),
                severity=severity,
                confidence=confidence,
                complexity_note=(
                    "estimate: O(n·m) comparisons for n loop iterations over a list of m items; "
                    "a set/dict makes each test O(1) on average."
                ),
            )
            return

    def _list_evidence(
        self, container: ast.expr, scope: ScopeTracker
    ) -> tuple[str, Severity, Confidence] | None:
        if isinstance(container, ast.List):
            if len(container.elts) < SMALL_LITERAL:
                return None
            return "scans a list literal", Severity.LOW, Confidence.HIGH
        if isinstance(container, ast.ListComp):
            return (
                "rebuilds the list with a comprehension on every evaluation before scanning it",
                Severity.MEDIUM,
                Confidence.HIGH,
            )
        if isinstance(container, ast.Call) and scope.qualify(container.func) in {"list", "sorted"}:
            return (
                "copies data into a new list before scanning it",
                Severity.MEDIUM,
                Confidence.HIGH,
            )
        if scope.kind(container) is not Kind.LIST:
            return None
        if isinstance(container, ast.Name):
            value = scope.module_value(container.id)
            if isinstance(value, ast.List) and scope.is_constant(value):
                if len(value.elts) < SMALL_LITERAL:
                    return None
                return f"scans the constant list `{container.id}`", Severity.LOW, Confidence.HIGH
            loop = scope.innermost_loop
            if loop and any(grows_in_loop(container.id, frame) for frame in scope.loops):
                return (
                    f"scans `{container.id}`, a list that also grows inside the loop",
                    Severity.MEDIUM,
                    Confidence.HIGH,
                )
            confidence = Confidence.MEDIUM if scope.is_param(container.id) else Confidence.HIGH
            return f"scans `{container.id}`, which is a list", Severity.MEDIUM, confidence
        return f"scans `{short(container)}`, which is a list", Severity.MEDIUM, Confidence.MEDIUM


def _unwrap_iter(node: ast.expr) -> ast.expr:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.args
        and node.func.id in {"enumerate", "reversed", "list", "tuple"}
    ):
        return node.args[0]
    return node


def _equality_join(tests: list[ast.expr], outer: set[str], inner: set[str]) -> ast.Compare | None:
    for test in tests:
        for node in ast.walk(test):
            if not (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
            ):
                continue
            left, right = names_in(node.left), names_in(node.comparators[0])
            if (left & outer and right & inner and not left & inner and not right & outer) or (
                left & inner and right & outer and not left & outer and not right & inner
            ):
                return node
    return None


class NestedLoopJoin(BaseRule):
    meta = RuleMeta(
        "PP-LOOP-002", "Nested loops matching items by equality", FindingCategory.NESTED_LOOPS
    )
    suggestion = (
        "Index the inner collection once by the join key, e.g. `by_key = {b.key: b for b in B}` "
        "(a dict of lists for non-unique keys), then look up `by_key.get(a.key)` in a single loop."
    )
    tradeoffs = (
        "Join keys must be hashable; duplicate keys need a dict of lists to keep every match; the "
        "index costs O(m) memory; result order can differ from the nested-loop order unless you "
        "preserve it explicitly."
    )

    def _outer(self, scope: ScopeTracker) -> LoopFrame | None:
        loop = scope.innermost_loop
        if loop is None or not isinstance(loop.node, ast.For | ast.AsyncFor):
            return None
        return loop

    def visit_For(self, node: ast.For, scope: ScopeTracker) -> None:
        outer = self._outer(scope)
        if outer is None or not isinstance(outer.node, ast.For | ast.AsyncFor):
            return
        outer_targets = names_in(outer.node.target)
        inner_iter = _unwrap_iter(node.iter)
        if names_in(inner_iter) & outer_targets or self._trivial_iter(inner_iter, scope):
            return
        inner_targets = names_in(node.target)
        tests = [n.test for n in iter_own_nodes(list(node.body)) if isinstance(n, ast.If)]
        match = _equality_join(tests, outer_targets, inner_targets)
        if match is not None:
            self._report(scope, node, match, outer.node, node.iter)

    def visit_GeneratorExp(self, node: ast.GeneratorExp, scope: ScopeTracker) -> None:
        self._comprehension(node, scope)

    def visit_ListComp(self, node: ast.ListComp, scope: ScopeTracker) -> None:
        self._comprehension(node, scope)

    def _comprehension(self, node: ast.GeneratorExp | ast.ListComp, scope: ScopeTracker) -> None:
        outer = self._outer(scope)
        if outer is None or len(node.generators) != 1:
            return
        assert isinstance(outer.node, ast.For | ast.AsyncFor)
        gen = node.generators[0]
        outer_targets = names_in(outer.node.target)
        inner_iter = _unwrap_iter(gen.iter)
        if names_in(inner_iter) & outer_targets or self._trivial_iter(inner_iter, scope):
            return
        match = _equality_join([*gen.ifs, node.elt], outer_targets, names_in(gen.target))
        if match is not None:
            self._report(scope, node, match, outer.node, gen.iter)

    @staticmethod
    def _trivial_iter(node: ast.expr, scope: ScopeTracker) -> bool:
        if isinstance(node, ast.Tuple | ast.List | ast.Set | ast.Dict):
            return len(getattr(node, "elts", getattr(node, "keys", []))) < SMALL_LITERAL
        return scope.kind(node) in {Kind.STR, Kind.DICT, Kind.SET}

    def _report(
        self,
        scope: ScopeTracker,
        node: ast.AST,
        match: ast.Compare,
        outer: ast.For | ast.AsyncFor,
        inner_iter: ast.expr,
    ) -> None:
        self.emit(
            scope,
            node,
            line_end=max(getattr(node, "lineno", 1), match.end_lineno or match.lineno),
            description=(
                f"For every item of `{short(outer.iter)}` (line {outer.lineno}) this code scans "
                f"all of `{short(inner_iter)}` looking for `{short(match)}`. An equality match "
                "found by scanning is a join; the number of comparisons is the product of both "
                "collection sizes."
            ),
            severity=Severity.HIGH,
            confidence=Confidence.HIGH,
            complexity_note=(
                "estimate: O(n·m) comparisons for n outer and m inner items; a dict index built "
                "once makes it O(n + m)."
            ),
        )


CONTAINER_COPIES = frozenset({"list", "tuple", "set", "frozenset", "dict", "sorted"})
REDUCTIONS = frozenset({"sum", "min", "max", "any", "all"})
PARSERS = frozenset(
    {"json.loads", "json.load", "yaml.safe_load", "yaml.load", "pickle.loads", "pickle.load"}
    | {"tomllib.loads", "ast.parse", "xml.etree.ElementTree.fromstring"}
)
FILE_LISTING = frozenset({"os.listdir", "os.scandir", "glob.glob", "os.walk"})
READ_METHODS = frozenset({"read", "readlines", "read_text", "read_bytes"})


class LoopInvariantWork(BaseRule):
    meta = RuleMeta(
        "PP-LOOP-003",
        "Loop-invariant computation repeated every iteration",
        FindingCategory.REPEATED_PROCESSING,
    )
    suggestion = "Compute the value once before the loop and reuse it inside the loop."
    tradeoffs = (
        "Only valid if the inputs really do not change during the loop (including through aliases, "
        "other threads, or the file system); hoisting evaluates the expression even when the loop "
        "body never runs and moves where exceptions are raised."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        loop = scope.innermost_loop
        if loop is None:
            return
        qualified = scope.qualify(node.func) or ""
        label = self._label(node, qualified, scope)
        if label is None:
            return
        what, severity, is_io = label
        if is_io and loop.kind == "while":
            return  # polling loops legitimately re-read
        if not self._args_invariant(node, loop, scope):
            return
        if self._fresh_copy_needed(node, loop, scope):
            return
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` ({what}) is evaluated on every iteration of the loop at line "
                f"{loop.line}, but none of its inputs change inside that loop, so every iteration "
                "after the first repeats identical work."
            ),
            severity=severity,
            confidence=Confidence.MEDIUM,
            complexity_note=(
                "estimate: the call's cost is paid once per iteration instead of once in total."
            ),
        )

    def _label(
        self, node: ast.Call, qualified: str, scope: ScopeTracker
    ) -> tuple[str, Severity, bool] | None:
        has_input = bool(node.args) and not all(
            isinstance(a, ast.Constant | ast.List | ast.Tuple | ast.Dict | ast.Set)
            for a in node.args
        )
        if qualified in CONTAINER_COPIES and has_input:
            return "copy/sort of a collection", Severity.MEDIUM, False
        if qualified in REDUCTIONS and has_input:
            return "full pass over a collection", Severity.MEDIUM, False
        if qualified in PARSERS and node.args:
            return "parsing", Severity.MEDIUM, False
        if qualified.startswith("pandas.read_") or qualified in FILE_LISTING:
            return "file system read", Severity.HIGH, True
        if isinstance(node.func, ast.Attribute) and node.func.attr in READ_METHODS:
            receiver = node.func.value
            if isinstance(receiver, ast.Call) and scope.qualify(receiver.func) in {
                "open",
                "pathlib.Path",
                "Path",
            }:
                return "file read", Severity.HIGH, True
        return None

    @staticmethod
    def _args_invariant(node: ast.Call, loop: LoopFrame, scope: ScopeTracker) -> bool:
        call = node
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
            call = node.func.value  # open(path).read(): the inputs are open()'s arguments
        parts: list[ast.expr] = [*call.args, *(k.value for k in call.keywords)]
        return all(scope.is_invariant(p, loop) for p in parts)

    @staticmethod
    def _fresh_copy_needed(node: ast.Call, loop: LoopFrame, scope: ScopeTracker) -> bool:
        parent = scope.parent
        if isinstance(parent, ast.Assign):
            names = {t.id for t in parent.targets if isinstance(t, ast.Name)}
            return bool(names & loop.mutated) or any(grows_in_loop(n, loop) for n in names)
        return isinstance(parent, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in parent.ops
        )


class SortInLoop(BaseRule):
    meta = RuleMeta(
        "PP-LOOP-004", "Re-sorting a growing collection inside a loop", FindingCategory.ALGORITHMIC
    )
    suggestion = (
        "Sort once after the loop, or keep the collection ordered incrementally with "
        "`bisect.insort` / a `heapq` priority queue when the order is needed during the loop."
    )
    tradeoffs = (
        "`heapq` only guarantees the smallest item is first (not a fully sorted list); "
        "`bisect.insort` is O(n) per insert because of list shifting but avoids repeated "
        "O(n log n) sorts; sort stability and key functions must be preserved."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        loop = scope.innermost_loop
        if loop is None or loop.kind == "comprehension":
            return
        target: ast.expr | None = None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "sort" and not node.args:
            target = node.func.value
        elif scope.qualify(node.func) == "sorted" and node.args:
            target = node.args[0]
        if not isinstance(target, ast.Name):
            return
        name = target.id
        if not grows_in_loop(name, loop) or reassigned_in_loop(name, loop):
            return
        if scope.kind(target) not in {Kind.LIST, Kind.UNKNOWN}:
            return
        self.emit(
            scope,
            node,
            description=(
                f"`{name}` grows inside the loop at line {loop.line} and is sorted again on "
                "every iteration. Each sort is O(k log k) for the current size k, so the total "
                "work grows faster than the number of items."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.HIGH,
            complexity_note="estimate: O(n² log n) total for n iterations vs O(n log n) once.",
        )
