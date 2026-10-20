"""Network rules: HTTP requests in loops and HTTP clients created per call/iteration."""

from __future__ import annotations

import ast
import re

from backend.audit.rules.base import BaseRule, LoopFrame, RuleMeta, ScopeTracker
from backend.audit.rules.kinds import HTTP_CLIENT_CONSTRUCTORS, Kind
from backend.audit.rules.loops import names_in, short
from backend.models.enums import Confidence, FindingCategory, Severity

HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request"})
MODULE_HTTP_FUNCTIONS = frozenset(
    {f"requests.{v}" for v in HTTP_VERBS}
    | {f"httpx.{v}" for v in HTTP_VERBS}
    | {"urllib.request.urlopen"}
)
RETRY_NAME = re.compile(r"(?i)attempt|retr|tries|backoff")
FACTORY_NAME = re.compile(
    r"(?i)^(__init__|__aenter__|__enter__|__post_init__|lifespan|main|setup|startup|on_startup)$"
    r"|^_?(get|create|make|build|init|new|open|setup|start)_|client|session"
)
MAX_RETRY_RANGE = 10


def _is_retry_loop(loop: LoopFrame) -> bool:
    node = loop.node
    if not isinstance(node, ast.For | ast.AsyncFor):
        return False
    if any(RETRY_NAME.search(n) for n in names_in(node.target) | names_in(node.iter)):
        return True
    it = node.iter
    if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "range"):
        return False
    bound = it.args[-1] if it.args else None
    return (
        isinstance(bound, ast.Constant)
        and isinstance(bound.value, int)
        and bound.value <= MAX_RETRY_RANGE
    )


class NetworkCallInLoop(BaseRule):
    meta = RuleMeta("PP-NET-001", "Sequential HTTP request inside a loop", FindingCategory.NETWORK)
    loop_based = True
    suggestion = (
        "Use a batch endpoint if the API has one; otherwise issue the requests concurrently with a "
        "bound (`asyncio.gather` + `asyncio.Semaphore`, or a `ThreadPoolExecutor` for sync "
        "clients), reuse one client/session, and cache repeated responses."
    )
    tradeoffs = (
        "Concurrency increases load on the remote service and can trigger rate limits; error "
        "handling, retries and result ordering get more complex; cached responses can be stale."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        loops = [frame for frame in scope.loops if frame.kind != "while"]
        if not loops or any(_is_retry_loop(frame) for frame in loops):
            return
        qualified = scope.qualify(node.func) or ""
        is_request = qualified in MODULE_HTTP_FUNCTIONS or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in HTTP_VERBS | {"stream"}
            and scope.kind(node.func.value) in {Kind.HTTP_CLIENT, Kind.ASYNC_HTTP_CLIENT}
        )
        if not is_request:
            return
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` issues an HTTP request on every iteration of the loop at line "
                f"{loops[-1].line}, one after another. Total time grows with the number of "
                "iterations multiplied by the request latency."
            ),
            severity=Severity.MEDIUM,
            confidence=Confidence.HIGH,
            complexity_note="estimate: N iterations → N sequential network round-trips.",
        )


class ClientPerCall(BaseRule):
    meta = RuleMeta(
        "PP-NET-002", "New HTTP client created per call or per iteration", FindingCategory.NETWORK
    )
    suggestion = (
        "Create one client/session per process (at startup, in a FastAPI lifespan/dependency, or "
        "on a long-lived object) and reuse it so connections and TLS sessions are pooled."
    )
    tradeoffs = (
        "A shared client must be closed on shutdown and configured once (timeouts, headers, auth); "
        "`httpx.AsyncClient` is bound to one event loop; sharing sync sessions across threads "
        "needs care."
    )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        qualified = scope.qualify(node.func) or ""
        if qualified not in HTTP_CLIENT_CONSTRUCTORS:
            return
        function = scope.function
        if scope.in_loop:
            self.emit(
                scope,
                node,
                description=(
                    f"`{short(node)}` creates a new HTTP client (with its own connection pool) on "
                    f"every iteration of the loop at line {scope.loops[-1].line}, so connections "
                    "and TLS handshakes are not reused between iterations."
                ),
                severity=Severity.MEDIUM,
                confidence=Confidence.HIGH,
            )
            return
        if function is None or function.kind != "function":
            return
        name = getattr(function.node, "name", "")
        if FACTORY_NAME.search(name) or self._escapes(node, scope):
            return
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` creates a new HTTP client every time `{name}` is called. "
                "Each client opens its own connections, so repeated calls cannot reuse pooled "
                "TCP/TLS connections."
            ),
            severity=Severity.LOW,
            confidence=Confidence.MEDIUM,
        )

    @staticmethod
    def _escapes(node: ast.Call, scope: ScopeTracker) -> bool:
        """The client is returned, yielded, or stored beyond the call (so it is reused)."""
        parent = scope.parent
        if isinstance(parent, ast.Return | ast.Yield | ast.YieldFrom):
            return True
        if isinstance(parent, ast.Assign):
            if any(isinstance(t, ast.Attribute | ast.Subscript) for t in parent.targets):
                return True
            names = {t.id for t in parent.targets if isinstance(t, ast.Name)}
            function = scope.function
            if function is not None and names & function.declared_outer:
                return True
            body = getattr(function.node, "body", []) if function else []
            for stmt in ast.walk(ast.Module(body=list(body), type_ignores=[])):
                if (
                    isinstance(stmt, ast.Return | ast.Yield)
                    and stmt.value is not None
                    and names & names_in(stmt.value)
                ):
                    return True
        return False
