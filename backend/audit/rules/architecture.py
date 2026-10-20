"""Architecture rules: heavy work at import time and unbounded module-level caches."""

from __future__ import annotations

import ast
import re

from backend.audit.rules.base import BaseRule, RuleMeta, ScopeTracker
from backend.audit.rules.kinds import SYNC_DB_CONNECT, Kind
from backend.audit.rules.loops import short
from backend.indexing.code_index import is_test_path
from backend.indexing.names import root_name
from backend.indexing.source import ParsedModule
from backend.models.enums import Confidence, FindingCategory, Severity

HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "request")
IMPORT_TIME_CALLS: dict[str, tuple[str, Severity, Confidence]] = {
    "urllib.request.urlopen": ("makes a network request", Severity.HIGH, Confidence.HIGH),
    "time.sleep": ("sleeps", Severity.MEDIUM, Confidence.HIGH),
    "subprocess.run": ("runs a subprocess", Severity.MEDIUM, Confidence.HIGH),
    "subprocess.check_output": ("runs a subprocess", Severity.MEDIUM, Confidence.HIGH),
    "subprocess.call": ("runs a subprocess", Severity.MEDIUM, Confidence.HIGH),
    "subprocess.Popen": ("starts a subprocess", Severity.MEDIUM, Confidence.HIGH),
    "os.system": ("runs a shell command", Severity.MEDIUM, Confidence.HIGH),
    "pickle.load": ("deserializes a file", Severity.MEDIUM, Confidence.HIGH),
    "joblib.load": ("loads a serialized model/data file", Severity.MEDIUM, Confidence.HIGH),
    "torch.load": ("loads a model file", Severity.MEDIUM, Confidence.HIGH),
    "numpy.load": ("loads an array file", Severity.MEDIUM, Confidence.HIGH),
    "json.load": ("reads and parses a file", Severity.LOW, Confidence.HIGH),
    "open": ("opens a file", Severity.LOW, Confidence.MEDIUM),
}
for _verb in HTTP_VERBS:
    for _lib in ("requests", "httpx"):
        IMPORT_TIME_CALLS[f"{_lib}.{_verb}"] = (
            "makes a network request",
            Severity.HIGH,
            Confidence.HIGH,
        )
for _connect in SYNC_DB_CONNECT:
    IMPORT_TIME_CALLS[_connect] = ("opens a database connection", Severity.MEDIUM, Confidence.HIGH)
BUILD_SCRIPTS = frozenset({"setup.py", "conftest.py", "noxfile.py", "fabfile.py", "conf.py"})
LARGE_RANGE = 10_000
CACHE_NAME = re.compile(r"(?i)cache|memo")
EVICTION_METHODS = frozenset({"pop", "popitem", "clear", "move_to_end"})


class ImportTimeWork(BaseRule):
    meta = RuleMeta("PP-ARCH-001", "Heavy work at import time", FindingCategory.ARCHITECTURE)
    suggestion = (
        "Defer the work until it is needed: move it into a function (optionally cached with "
        "`functools.cache`), an application startup hook/lifespan, or a lazily initialized "
        "attribute."
    )
    tradeoffs = (
        "Lazy initialization moves the cost and any errors to first use (the first request may be "
        "slower); concurrent lazy init needs a lock; some frameworks expect module-level objects."
    )

    def begin_module(self, module: ParsedModule) -> None:
        name = module.path.rsplit("/", 1)[-1]
        self._enabled = name not in BUILD_SCRIPTS and not is_test_path(module.path)

    def visit_Call(self, node: ast.Call, scope: ScopeTracker) -> None:
        if not self._enabled or not scope.at_import_time:
            return
        qualified = scope.qualify(node.func) or ""
        verdict = IMPORT_TIME_CALLS.get(qualified)
        if qualified.startswith("pandas.read_"):
            verdict = ("reads a data file into a DataFrame", Severity.MEDIUM, Confidence.HIGH)
        if verdict is None and qualified == "range" and node.args:
            bound = node.args[-1] if len(node.args) == 1 else node.args[1]
            if (
                isinstance(bound, ast.Constant)
                and isinstance(bound.value, int)
                and bound.value >= LARGE_RANGE
                and scope.is_loop_iterable(node)
            ):
                verdict = (f"iterates {bound.value:,} times", Severity.LOW, Confidence.HIGH)
        if verdict is None:
            return
        what, severity, confidence = verdict
        self.emit(
            scope,
            node,
            description=(
                f"`{short(node)}` {what} while the module is being imported. Every process that "
                "imports this module (web workers, CLI commands, tests) pays this cost and "
                "inherits its failure modes before any code path needs the result."
            ),
            severity=severity,
            confidence=confidence,
        )


class UnboundedModuleCache(BaseRule):
    meta = RuleMeta("PP-ARCH-002", "Unbounded module-level cache", FindingCategory.ARCHITECTURE)
    suggestion = (
        "Bound the cache: `functools.lru_cache(maxsize=...)`, an LRU/TTL cache (for example "
        "`cachetools.TTLCache`), or explicit eviction when it exceeds a size."
    )
    tradeoffs = (
        "Eviction causes recomputation on misses; TTL caches can serve stale values until they "
        "expire; caches shared between threads need locking or atomic operations."
    )

    def end_module(self, scope: ScopeTracker) -> None:
        candidates: dict[str, ast.stmt] = {}
        for stmt in scope.tree.body:
            target: ast.expr | None = None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            if (
                isinstance(target, ast.Name)
                and CACHE_NAME.search(target.id)
                and scope.infer.expr_kind(value, lambda _n: Kind.UNKNOWN, lambda _a: Kind.UNKNOWN)
                is Kind.DICT
            ):
                candidates[target.id] = stmt
        if not candidates:
            return
        writes: set[str] = set()
        evicted: set[str] = set()
        for function in ast.walk(scope.tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                self._classify_use(node, candidates, writes, evicted)
        for name in sorted(writes - evicted):
            stmt = candidates[name]
            self.emit(
                scope,
                stmt,
                description=(
                    f"`{name}` is a module-level dict that functions add entries to, but nothing "
                    "in this module removes entries or checks its size. If keys come from an "
                    "unbounded domain (user IDs, request parameters), memory grows for the "
                    "lifetime of the process."
                ),
                severity=Severity.MEDIUM,
                confidence=Confidence.MEDIUM,
            )

    @staticmethod
    def _classify_use(
        node: ast.AST, candidates: dict[str, ast.stmt], writes: set[str], evicted: set[str]
    ) -> None:
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            name = node.value.id
            if name in candidates:
                if isinstance(node.ctx, ast.Store):
                    writes.add(name)
                elif isinstance(node.ctx, ast.Del):
                    evicted.add(name)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                name = func.value.id
                if name in candidates:
                    if func.attr in {"setdefault", "update", "__setitem__"}:
                        writes.add(name)
                    elif func.attr in EVICTION_METHODS:
                        evicted.add(name)
            elif isinstance(func, ast.Name) and func.id == "len" and node.args:
                if (root := root_name(node.args[0])) in candidates:
                    evicted.add(root)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in candidates:
                evicted.add(node.id)  # rebinding (with `global`) resets the cache
