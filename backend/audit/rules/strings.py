"""String building rules."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, LoopFrame, RuleMeta, ScopeTracker
from backend.audit.rules.kinds import Kind
from backend.audit.rules.loops import names_in, reassigned_in_loop
from backend.models.enums import Confidence, FindingCategory, Severity


def _concat_operands(name: str, nodes: list[ast.AST]) -> set[int]:
    """ids of the ``name`` occurrences that only express the accumulation itself: the target of
    ``name += ...`` and both sides' ``name`` in ``name = name + ...`` / ``name = ... + name``."""
    own: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                own.add(id(node.target))
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.BinOp)
            and isinstance(node.value.op, ast.Add)
        ):
            sides = [node.value.left, node.value.right]
            operand = next((s for s in sides if isinstance(s, ast.Name) and s.id == name), None)
            if operand is not None:
                own |= {id(node.targets[0]), id(operand)}
    return own


def read_in_loop(name: str, loop: LoopFrame) -> bool:
    """True when ``name`` is read on every pass of ``loop`` (its body, or a ``while`` test) other
    than by the accumulation statements themselves: yielded, passed to a call, compared, sliced.
    Reads in nested functions count too, since they see the partial value."""
    node = loop.node
    region: list[ast.AST] = []
    if isinstance(node, ast.For | ast.AsyncFor | ast.While):
        region = [*node.body]
        if isinstance(node, ast.While):
            region.insert(0, node.test)
    nodes = [child for item in region for child in ast.walk(item)]
    own = _concat_operands(name, nodes)
    return any(
        isinstance(child, ast.Name)
        and child.id == name
        and isinstance(child.ctx, ast.Load)
        and id(child) not in own
        for child in nodes
    )


class StringConcatInLoop(BaseRule):
    meta = RuleMeta(
        "PP-STR-001", "String built by repeated concatenation in a loop", FindingCategory.STRINGS
    )
    loop_based = True
    suggestion = (
        "Collect the pieces in a list and call `''.join(parts)` once after the loop, or write "
        "them to an `io.StringIO`."
    )
    tradeoffs = (
        "All pieces stay in memory until the join (similar peak to the final string); output is "
        "identical for str; for very few iterations the difference is negligible."
    )

    def visit_AugAssign(self, node: ast.AugAssign, scope: ScopeTracker) -> None:
        if isinstance(node.op, ast.Add):
            self._check(node, node.target, node.value, scope)

    def visit_Assign(self, node: ast.Assign, scope: ScopeTracker) -> None:
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return
        value = node.value
        if not (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add)):
            return
        name = node.targets[0].id
        if isinstance(value.left, ast.Name) and value.left.id == name:
            self._check(node, node.targets[0], value.right, scope)
        elif isinstance(value.right, ast.Name) and value.right.id == name:
            self._check(node, node.targets[0], value.left, scope)

    def _check(
        self, node: ast.stmt, target: ast.expr, piece: ast.expr, scope: ScopeTracker
    ) -> None:
        loop = scope.innermost_loop
        if loop is None or loop.kind == "comprehension" or not isinstance(target, ast.Name):
            return
        target_kind = scope.kind(target)
        piece_kind = scope.kind(piece)
        if target_kind is not Kind.STR and not (
            target_kind is Kind.UNKNOWN and piece_kind is Kind.STR
        ):
            return
        if piece_kind not in {Kind.STR, Kind.UNKNOWN}:
            return
        own_targets = getattr(loop.node, "target", None)
        if reassigned_in_loop(target.id, loop) or (
            own_targets is not None and target.id in names_in(own_targets)
        ):
            return  # a fresh string every iteration, not an accumulator
        if read_in_loop(target.id, loop):
            return  # the partial string is consumed every iteration; a join would be repeated
        self.emit(
            scope,
            node,
            description=(
                f"`{target.id}` is extended with `+` on every iteration of the loop at line "
                f"{loop.line}. Python strings are immutable, so each concatenation may allocate a "
                "new string and copy everything accumulated so far. CPython can sometimes resize "
                "in place (an implementation detail that requires no other references), so the "
                "real cost depends on the interpreter and the surrounding code."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.HIGH if target_kind is Kind.STR else Confidence.MEDIUM,
            complexity_note=(
                "estimate: up to O(n²) characters copied for n appended pieces when the string "
                "is not resized in place; ''.join is O(total length)."
            ),
        )
