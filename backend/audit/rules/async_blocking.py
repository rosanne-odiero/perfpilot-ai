"""Blocking calls made directly inside ``async def`` functions."""

from __future__ import annotations

import ast

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.audit.rules.database import DB_NAME, receiver_label
from backend.audit.rules.kinds import SYNC_DB_CONNECT, Kind
from backend.audit.rules.loops import short
from backend.indexing.ast_index import iter_own_nodes
from backend.indexing.source import ParsedModule
from backend.models.enums import Confidence, FindingCategory, Severity

HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "request"})

BLOCKING_CALLS: dict[str, tuple[str, Severity, Confidence]] = {
    "time.sleep": ("sleeps the whole thread", Severity.HIGH, Confidence.HIGH),
    "urllib.request.urlopen": ("performs a blocking HTTP request", Severity.HIGH, Confidence.HIGH),
    "subprocess.run": ("waits for a child process", Severity.HIGH, Confidence.HIGH),
    "subprocess.call": ("waits for a child process", Severity.HIGH, Confidence.HIGH),
    "subprocess.check_call": ("waits for a child process", Severity.HIGH, Confidence.HIGH),
    "subprocess.check_output": ("waits for a child process", Severity.HIGH, Confidence.HIGH),
    "os.system": ("waits for a shell command", Severity.HIGH, Confidence.HIGH),
    "socket.create_connection": ("opens a blocking socket", Severity.HIGH, Confidence.MEDIUM),
    "smtplib.SMTP": ("opens a blocking SMTP connection", Severity.HIGH, Confidence.MEDIUM),
    "smtplib.SMTP_SSL": ("opens a blocking SMTP connection", Severity.HIGH, Confidence.MEDIUM),
    "open": ("performs synchronous file I/O", Severity.LOW, Confidence.MEDIUM),
    "shutil.copyfile": ("performs synchronous file I/O", Severity.LOW, Confidence.MEDIUM),
    "shutil.copy": ("performs synchronous file I/O", Severity.LOW, Confidence.MEDIUM),
    "shutil.rmtree": ("performs synchronous file I/O", Severity.LOW, Confidence.MEDIUM),
}
for _verb in HTTP_VERBS | {"stream"}:
    BLOCKING_CALLS[f"requests.{_verb}"] = (
        "performs a blocking HTTP request",
        Severity.HIGH,
        Confidence.HIGH,
    )
    BLOCKING_CALLS[f"httpx.{_verb}"] = (
        "performs a blocking HTTP request (module-level httpx functions are synchronous)",
        Severity.HIGH,
        Confidence.HIGH,
    )
for _connect in SYNC_DB_CONNECT:
    BLOCKING_CALLS[_connect] = (
        "opens a synchronous database connection",
        Severity.HIGH,
        Confidence.HIGH,
    )

SYNC_SESSION_METHODS = frozenset(
    {"execute", "scalars", "scalar", "commit", "flush", "refresh", "get", "exec"}
)
_SESSION_PREFIX = "uses a synchronous database session/driver"
PATH_IO_METHODS = frozenset({"read_text", "read_bytes", "write_text", "write_bytes"})


class BlockingCallInAsync(BaseRule):
    meta = RuleMeta(
        "PP-ASYNC-001", "Blocking call inside an async function", FindingCategory.ASYNC_BLOCKING
    )
    suggestion = (
        "Use the async equivalent (`await asyncio.sleep`, `httpx.AsyncClient`/`aiohttp`, "
        "`asyncio.create_subprocess_exec`, an async DB driver or `AsyncSession`, `aiofiles`), or "
        "move the call off the event loop with `await asyncio.to_thread(func, ...)`."
    )
    tradeoffs = (
        "Thread offloading adds thread-pool overhead and requires the code to be thread-safe; "
        "async libraries have different APIs, timeouts and exceptions; very short blocking calls "
        "(small local files) may not be worth changing."
    )

    def begin_module(self, module: ParsedModule) -> None:
        self._awaited: dict[int, set[str]] = {}
        self._session_reported: set[tuple[int, str]] = set()

    def _awaited_receivers(self, scope: ScopeTracker) -> set[str]:
        function = scope.function
        if function is None:
            return set()
        key = id(function.node)
        if key not in self._awaited:
            labels: set[str] = set()
            body = getattr(function.node, "body", [])
            for node in iter_own_nodes(list(body)):
                if (
                    isinstance(node, ast.Await)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and (label := receiver_label(node.value.func.value))
                ):
                    labels.add(label)
            self._awaited[key] = labels
        return self._awaited[key]

    def _session_uses(self, scope: ScopeTracker, label: str) -> list[int]:
        """Lines of every non-awaited ORM/driver call on ``label`` in the current coroutine."""
        function = scope.function
        body = getattr(function.node, "body", []) if function else []
        awaited = {id(n.value) for n in iter_own_nodes(list(body)) if isinstance(n, ast.Await)}
        return sorted(
            {
                n.lineno
                for n in iter_own_nodes(list(body))
                if isinstance(n, ast.Call)
                and id(n) not in awaited
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in SYNC_SESSION_METHODS | {"query"}
                and receiver_label(n.func.value) == label
            }
        )

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if not scope.in_async_function or isinstance(scope.parent, ast.Await):
            return
        verdict = self._classify(node, scope)
        if verdict is None:
            return
        mechanism, severity, confidence = verdict
        function = scope.function
        name = getattr(function.node, "name", "?") if function else "?"
        if mechanism.startswith(_SESSION_PREFIX):
            assert isinstance(node.func, ast.Attribute)
            label = receiver_label(node.func.value) or ""
            key = (id(function.node) if function else 0, label)
            if key in self._session_reported:
                return  # one finding per coroutine and session object
            self._session_reported.add(key)
            lines = self._session_uses(scope, label)
            if len(lines) > 1:
                listed = ", ".join(str(n) for n in lines[:8]) + (", …" if len(lines) > 8 else "")
                mechanism += f"; `{label}` is used this way {len(lines)} times (lines {listed})"
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` {mechanism} inside `async def {name}`. While it runs, the event "
                "loop thread is blocked, so other coroutines (concurrent requests, timers, "
                "heartbeats) cannot make progress until it returns."
            ),
            severity=severity,
            confidence=confidence,
        )

    def _classify(
        self, node: ast.Call, scope: ScopeTracker
    ) -> tuple[str, Severity, Confidence] | None:
        qualified = scope.qualify(node.func) or ""
        if qualified in BLOCKING_CALLS:
            return BLOCKING_CALLS[qualified]
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        method = func.attr
        receiver = func.value
        receiver_kind = scope.kind(receiver)
        if receiver_kind is Kind.HTTP_CLIENT and method in HTTP_VERBS | {"send", "stream"}:
            return "performs a blocking HTTP request", Severity.HIGH, Confidence.HIGH
        if receiver_kind in {Kind.DB_CONNECTION, Kind.DB_CURSOR} and method in {
            "execute",
            "executemany",
            "executescript",
            "fetchall",
            "fetchone",
            "fetchmany",
            "commit",
        }:
            return "runs a synchronous database call", Severity.HIGH, Confidence.HIGH
        if receiver_kind is Kind.FUTURE and method == "result":
            return "waits on a concurrent future", Severity.HIGH, Confidence.HIGH
        if (
            method in PATH_IO_METHODS
            and isinstance(receiver, ast.Call)
            and scope.qualify(receiver.func) in {"pathlib.Path", "Path"}
        ):
            return "performs synchronous file I/O", Severity.LOW, Confidence.MEDIUM
        label = receiver_label(receiver)
        if label and DB_NAME.search(label) and label not in self._awaited_receivers(scope):
            if method == "query":
                return (
                    f"{_SESSION_PREFIX} (`AsyncSession` has no `.query()`)",
                    Severity.HIGH,
                    Confidence.MEDIUM,
                )
            if method in SYNC_SESSION_METHODS and receiver_kind is not Kind.ASYNC_HTTP_CLIENT:
                if method == "get" and not (node.args and _capitalized(node.args[0])):
                    return None
                return (
                    f"{_SESSION_PREFIX} (database calls on it are never awaited)",
                    Severity.HIGH,
                    Confidence.MEDIUM,
                )
        return None


def _capitalized(node: ast.expr) -> bool:
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
    return bool(name[:1].isupper())
