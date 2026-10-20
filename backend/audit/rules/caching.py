"""Caching opportunities: uncached overlapping recursion and repeated identical expensive calls."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.audit.rules.loops import short
from backend.indexing.ast_index import iter_own_nodes
from backend.models.enums import Confidence, FindingCategory, Severity

CACHE_DECORATORS = frozenset(
    {"functools.lru_cache", "functools.cache", "functools.cached_property", "lru_cache", "cache"}
)
IMPURE_CALLS = frozenset({"print", "open", "input", "setattr", "delattr", "exec", "eval"})
EXPENSIVE_CALLS = frozenset(
    {"json.load", "json.loads", "yaml.safe_load", "yaml.load", "pickle.load", "pickle.loads"}
    | {"os.listdir", "glob.glob", "os.walk", "re.compile", "subprocess.run"}
    | {"subprocess.check_output", "requests.get", "httpx.get", "urllib.request.urlopen"}
    | {"tomllib.loads", "csv.reader"}
)


def _is_cached(node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ScopeTracker) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        qualified = scope.qualify(target) or ""
        if qualified in CACHE_DECORATORS or "cache" in qualified.rsplit(".", 1)[-1].lower():
            return True
    return False


def _param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = node.args
    return {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}


def _is_index_arithmetic(expr: ast.expr, params: set[str]) -> bool:
    """Arguments like ``n - 1``, ``i + 1``, ``k`` or constants: overlapping subproblems."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            if node.id not in params:
                return False
        elif not isinstance(
            node, ast.Constant | ast.BinOp | ast.UnaryOp | ast.operator | ast.unaryop | ast.Load
        ):
            return False
    return True


def _is_pure(node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ScopeTracker) -> bool:
    for sub in iter_own_nodes(list(node.body)):
        if isinstance(sub, ast.Global | ast.Nonlocal | ast.Yield | ast.YieldFrom | ast.Await):
            return False
        if isinstance(sub, ast.Attribute | ast.Subscript) and isinstance(
            sub.ctx, ast.Store | ast.Del
        ):
            return False
        if isinstance(sub, ast.Call):
            qualified = scope.qualify(sub.func) or ""
            if qualified in IMPURE_CALLS or isinstance(sub.func, ast.Attribute):
                return False
    return True


class RecursionWithoutCache(BaseRule):
    meta = RuleMeta(
        "PP-CACHE-001", "Overlapping recursion without memoization", FindingCategory.CACHING
    )
    suggestion = (
        "Memoize with `@functools.cache` / `@functools.lru_cache(maxsize=...)`, or rewrite the "
        "recursion bottom-up/iteratively."
    )
    tradeoffs = (
        "All arguments must be hashable; the cache keeps every distinct argument tuple and result "
        "alive (bound it with maxsize); memoization is only correct for pure functions (same "
        "inputs, same output, no side effects); the recursion depth limit still applies."
    )

    def visit_FunctionDef(self, node: ast.FunctionDef, scope: ScopeTracker) -> None:
        if scope.frame.kind == "class" or _is_cached(node, scope):
            return
        params = _param_names(node)
        if not params:
            return
        self_calls = [
            call
            for call in iter_own_nodes(list(node.body))
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == node.name
        ]
        if len(self_calls) < 2 or not _is_pure(node, scope):
            return
        if not all(
            _is_index_arithmetic(arg, params)
            for call in self_calls
            for arg in [*call.args, *(k.value for k in call.keywords)]
        ):
            return
        self.emit(
            scope,
            node,
            line_end=node.lineno,
            symbol=scope.qualname_for(node.name),
            description=(
                f"`{node.name}` calls itself {len(self_calls)} times per invocation with "
                "arguments derived arithmetically from its parameters, and it has no cache. "
                "Such tree recursion typically recomputes the same subproblems many times."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.MEDIUM,
            complexity_note=(
                "estimate: without memoization the call count can grow exponentially with the "
                "input (e.g. O(2^n)); with it, proportional to the number of distinct arguments."
            ),
        )


class RepeatedConstantCall(BaseRule):
    meta = RuleMeta(
        "PP-CACHE-002",
        "Same expensive call repeated with identical constant arguments",
        FindingCategory.CACHING,
    )
    suggestion = (
        "Call it once and reuse the result within the function; if the result is stable across "
        "calls, compute it once at module level or cache it with `functools.cache`."
    )
    tradeoffs = (
        "Reusing the result assumes the underlying file, data, or remote resource does not change "
        "between the calls; a module-level cache can serve stale data and holds memory."
    )

    def visit_FunctionDef(self, node: ast.FunctionDef, scope: ScopeTracker) -> None:
        self._check(node, scope)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef, scope: ScopeTracker) -> None:
        self._check(node, scope)

    def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef, scope: ScopeTracker) -> None:
        seen: dict[str, ast.Call] = {}
        for call in iter_own_nodes(list(node.body)):
            if not isinstance(call, ast.Call):
                continue
            qualified = scope.qualify(call.func) or ""
            if qualified not in EXPENSIVE_CALLS or not call.args:
                continue
            arguments = [*call.args, *(k.value for k in call.keywords)]
            if not all(scope.is_constant(a) for a in arguments):
                continue
            key = qualified + ast.dump(ast.Tuple(elts=arguments, ctx=ast.Load()))
            first = seen.get(key)
            if first is None:
                seen[key] = call
                continue
            self.emit(
                scope,
                call,
                symbol=scope.qualname_for(node.name),
                description=(
                    f"`{short(call)}` is evaluated again with the same constant arguments as on "
                    f"line {first.lineno} in `{node.name}`; both calls do the same work."
                ),
                severity=Severity.LOW,
                confidence=Confidence.MEDIUM,
            )
