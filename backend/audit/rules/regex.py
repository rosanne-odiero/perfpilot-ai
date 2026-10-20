"""Regular-expression rules."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.models.enums import Confidence, FindingCategory, Severity

RE_FUNCTIONS = frozenset(
    {"match", "search", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
)


class RegexInLoop(BaseRule):
    meta = RuleMeta(
        "PP-REGEX-001", "Constant regex pattern processed inside a loop", FindingCategory.REGEX
    )
    suggestion = (
        "Compile the pattern once at module level (`PATTERN = re.compile(...)`) and call "
        "`PATTERN.search(...)` (or `.sub`, `.findall`, …) inside the loop."
    )
    tradeoffs = (
        "Module-level compilation runs at import time and keeps the compiled pattern alive for the "
        "life of the process; matching behavior is identical."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if not scope.in_loop:
            return
        qualified = scope.qualify(node.func) or ""
        module, _, func = qualified.rpartition(".")
        if module not in {"re", "regex"}:
            return
        pattern = node.args[0] if node.args else None
        for keyword in node.keywords:
            if keyword.arg == "pattern":
                pattern = keyword.value
        if pattern is None or not scope.is_constant(pattern):
            return
        if func == "compile":
            self.emit(
                scope,
                node,
                description=(
                    f"`{module}.compile` is called with a constant pattern on every iteration of "
                    f"the loop at line {scope.loops[-1].line}. The `re` module caches compiled "
                    "patterns, so each call is a cache lookup rather than a full compile, but the "
                    "lookup, argument handling and call overhead repeat for an object that never "
                    "changes."
                ),
                severity=Severity.LOW,
                confidence=Confidence.HIGH,
            )
        elif func in RE_FUNCTIONS:
            self.emit(
                scope,
                node,
                description=(
                    f"`{module}.{func}` receives a constant pattern on every iteration of the loop "
                    f"at line {scope.loops[-1].line}. CPython caches recently compiled patterns "
                    "(a bounded cache), so this mostly costs a cache lookup per call; it becomes "
                    "expensive only if many distinct patterns evict each other."
                ),
                severity=Severity.LOW,
                confidence=Confidence.LOW,
            )
