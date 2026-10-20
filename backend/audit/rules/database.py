"""Database access rules: queries issued once per loop iteration (N+1)."""

from __future__ import annotations

import ast
import re

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.audit.rules.kinds import Kind
from backend.audit.rules.loops import short
from backend.indexing.names import dotted_name, root_name
from backend.models.enums import Confidence, FindingCategory, Severity

DB_NAME = re.compile(
    r"(?i)^(db|session|db_session|dbsession|sess|cursor|cur|conn|connection|con|database|tx|"
    r"transaction|async_session|pool|supabase)$|_(db|session|cursor|conn|connection)$"
)
SESSION_METHODS = frozenset({"query", "execute", "scalars", "scalar", "exec"})
DRIVER_METHODS = frozenset({"execute", "fetch", "fetchrow", "fetchval"})
DJANGO_METHODS = frozenset(
    {"get", "filter", "exclude", "all", "first", "last", "count", "exists", "create", "aggregate"}
    | {"get_or_create", "update_or_create", "values", "values_list", "raw", "in_bulk"}
)
DJANGO_EAGER = frozenset({"get", "first", "last", "count", "exists", "create", "aggregate"})


def receiver_label(node: ast.expr) -> str | None:
    """Last name of the receiver: ``db`` for ``db``, ``session`` for ``self.session``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def classify_db_call(node: ast.Call, scope: ScopeTracker) -> tuple[str, Confidence] | None:
    """Return (description of the API, confidence) when ``node`` issues a database query."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    method = func.attr
    receiver = func.value
    chain = dotted_name(receiver) or ""
    if method in DJANGO_METHODS and (chain.endswith(".objects") or chain == "objects"):
        confidence = Confidence.HIGH if method in DJANGO_EAGER else Confidence.MEDIUM
        return f"Django ORM `.objects.{method}()`", confidence
    receiver_kind = scope.kind(receiver)
    if receiver_kind in {Kind.HTTP_CLIENT, Kind.ASYNC_HTTP_CLIENT}:
        return None
    if receiver_kind in {Kind.DB_CONNECTION, Kind.DB_CURSOR} and method in DRIVER_METHODS:
        return f"DB-API `{method}()`", Confidence.HIGH
    label = receiver_label(receiver)
    if label is None:
        if method == "execute" and DB_NAME.search(root_name(receiver) or ""):
            return "query builder `.execute()`", Confidence.MEDIUM
        return None
    if not DB_NAME.search(label):
        return None
    if method in SESSION_METHODS or method in DRIVER_METHODS:
        return f"`{label}.{method}()`", Confidence.MEDIUM
    if method == "get" and node.args and _looks_like_model(node.args[0]):
        return f"`{label}.get(<Model>, …)`", Confidence.MEDIUM
    return None


def _looks_like_model(node: ast.expr) -> bool:
    name = dotted_name(node) or ""
    return name.rsplit(".", 1)[-1][:1].isupper()


def _contains_db_call(node: ast.expr, scope: ScopeTracker) -> bool:
    return any(
        isinstance(sub, ast.Call) and classify_db_call(sub, scope) is not None
        for sub in ast.walk(node)
    )


class QueryInLoop(BaseRule):
    meta = RuleMeta("PP-DB-001", "Database query inside a loop (N+1)", FindingCategory.DATABASE)
    suggestion = (
        "Load what the loop needs with one query before it (`WHERE id IN (...)`, "
        "`select(...).where(Model.id.in_(ids))`, `selectinload`/`joinedload`, Django "
        "`select_related`/`prefetch_related`/`in_bulk`) and look rows up in a dict; batch writes "
        "with `executemany`, bulk inserts, or a single UPDATE."
    )
    tradeoffs = (
        "One large query can return more data and use more memory; IN-lists have database limits "
        "(chunk them); batching writes changes transaction and error-reporting granularity; "
        "eager loading can over-fetch when only a few rows are used."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        loops = [frame for frame in scope.loops if frame.kind != "while"]
        if not loops:
            return
        innermost = loops[-1]
        if isinstance(innermost.node, ast.For | ast.AsyncFor) and isinstance(
            innermost.node.iter, ast.Tuple | ast.List
        ):
            return  # fixed, small set of statements (e.g. DDL per table), not per-row queries
        verdict = classify_db_call(node, scope)
        if verdict is None:
            return
        assert isinstance(node.func, ast.Attribute)
        if _contains_db_call(node.func.value, scope):
            return  # the inner call in the chain is reported instead
        api, confidence = verdict
        self.emit(
            scope,
            node,
            description=(
                f"{api} (`{short(node)}`) runs once per iteration of the loop at line "
                f"{innermost.line}. When the loop walks rows or IDs, the number of database "
                "round-trips grows with the data (the N+1 query pattern), and each round-trip "
                "pays driver and network latency."
            ),
            severity=Severity.HIGH,
            confidence=confidence,
            complexity_note=(
                "estimate: N loop iterations issue N queries; a batched query needs 1-2 "
                "round-trips."
            ),
        )
