"""Execution-path criticality: which role does the code behind a finding play?

A finding inside a request handler matters more than the same pattern in a one-off seed script.
This module classifies every finding's enclosing code into a :class:`CodeRole` and records the
best-effort execution path that justifies the role:

1. **Path hints** win: tests, seed/fixture/migration code, examples/docs, and ``scripts/``-style
   directories are classified by their repository path alone.
2. **Entry points** from the index: HTTP routes and views are request handlers, Celery tasks are
   background tasks, CLI commands / ``__main__`` callees / console scripts are CLI entries.
3. **Call graph** (bounded multi-source BFS, ``MAX_PATH_DEPTH`` hops): code called from a request
   handler is on the request path; from a task, background; from a CLI entry, CLI.
4. **Import graph**: a module (transitively) imported by a handler or task module is library code
   that probably runs for requests, but the call path is unknown.
5. Anything else is internal code with no known path from an entry point.

Each edge on a call path is also checked for being invoked inside a loop of the caller ("hot
loop"): the callee then runs once per iteration, which prioritization folds into the loop factor.
The call graph is name-based and under-approximates real calls (see ``call_graph.py``), so roles
are evidence, not proof.
"""

from __future__ import annotations

import ast
import itertools
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import networkx as nx

from backend.indexing.ast_index import COMPREHENSIONS, ScopeNode
from backend.indexing.call_graph import symbol_id
from backend.indexing.code_index import is_test_path
from backend.indexing.source import ParsedModule
from backend.models.audit_schemas import CodeIndex, ModuleInfo, SymbolInfo
from backend.models.enums import CodeRole

MAX_PATH_DEPTH = 8

ROLE_FACTORS: dict[CodeRole, float] = {
    CodeRole.REQUEST_HANDLER: 1.0,
    CodeRole.REQUEST_PATH: 0.9,
    CodeRole.BACKGROUND_TASK: 0.9,
    CodeRole.LIBRARY: 0.75,
    CodeRole.CLI_SCRIPT: 0.5,
    CodeRole.INTERNAL: 0.5,
    CodeRole.SEED_MIGRATION: 0.35,
    CodeRole.EXAMPLE_DOCS: 0.3,
    CodeRole.TEST: 0.2,
}
# Without request handlers or background tasks the CLI is the product's primary interface.
CLI_PRIMARY_FACTOR = 1.0

_SEED_PATH = re.compile(
    r"(^|/)(alembic|migrations?|seeds?|seeders?|seed_data|fixtures?)/"
    r"|(^|/)(seeds?|seed_[^/]*|[^/]*_seeds?|seed[^/]*data)\.py$"
)
_EXAMPLE_PATH = re.compile(r"(^|/)(examples?|samples?|demos?|docs?|tutorials?|notebooks?)/")
_SCRIPT_PATH = re.compile(
    r"(^|/)(scripts?|bin|tools|benchmarks?|management/commands)/"
    r"|(^|/)(setup|noxfile|fabfile|manage)\.py$"
)
_BACKGROUND_PATH = re.compile(r"(^|/)(tasks|workers?|jobs|consumers?|schedulers?|cron)(/|\.py$)")

_HANDLER_REASONS = ("HTTP route", "Django view", "Django REST view", "Django class-based view")
_BACKGROUND_REASONS = ("Celery task",)
_CLI_REASONS = ("CLI command", "called from __main__ guard", "console script")
_ROUTE_DECORATOR = re.compile(
    r"\.(get|post|put|patch|delete|head|options|trace|route|api_route|websocket\w*)\(|api_view"
)
_TASK_DECORATOR = re.compile(r"(^|\.)(task|shared_task)\b")
_CLI_DECORATOR = re.compile(r"\.(command|group|callback)\b")


def path_hint(file_path: str) -> CodeRole | None:
    """Role implied by the repository path alone (most specific first)."""
    if is_test_path(file_path):
        return CodeRole.TEST
    if _SEED_PATH.search(file_path):
        return CodeRole.SEED_MIGRATION
    if _EXAMPLE_PATH.search(file_path):
        return CodeRole.EXAMPLE_DOCS
    if _SCRIPT_PATH.search(file_path):
        return CodeRole.CLI_SCRIPT
    if _BACKGROUND_PATH.search(file_path):
        return CodeRole.BACKGROUND_TASK
    return None


_DECISIVE_HINTS = frozenset(
    {CodeRole.TEST, CodeRole.SEED_MIGRATION, CodeRole.EXAMPLE_DOCS, CodeRole.CLI_SCRIPT}
)


def _symbol_reasons(module: ModuleInfo) -> dict[str, str]:
    """``qualname -> reason`` from the ``"<reason>: <qualname>"`` entries of the module."""
    reasons: dict[str, str] = {}
    for entry in module.entrypoint_reasons:
        reason, sep, name = entry.rpartition(": ")
        if sep and name:
            reasons.setdefault(name, reason)
    return reasons


def entry_role(
    module: ModuleInfo, symbol: SymbolInfo, reasons: Mapping[str, str]
) -> CodeRole | None:
    """Role of an entry-point symbol, from its detection reason (or decorators when the module's
    reason list was truncated). Application factories return ``None``: they run once at startup."""
    if not symbol.is_entrypoint:
        return None
    reason = reasons.get(symbol.qualname)
    if reason is not None:
        if reason.startswith(_HANDLER_REASONS):
            return CodeRole.REQUEST_HANDLER
        if reason.startswith(_BACKGROUND_REASONS):
            return CodeRole.BACKGROUND_TASK
        if reason.startswith(_CLI_REASONS):
            return CodeRole.CLI_SCRIPT
        return None
    decorators = " ".join(symbol.decorators)
    is_view_module = module.file_path.rsplit("/", 1)[-1] == "views.py"
    if _ROUTE_DECORATOR.search(decorators) or symbol.kind == "class" or is_view_module:
        return CodeRole.REQUEST_HANDLER
    if _TASK_DECORATOR.search(decorators):
        return CodeRole.BACKGROUND_TASK
    if _CLI_DECORATOR.search(decorators) or not decorators:
        return CodeRole.CLI_SCRIPT
    return None


@dataclass(frozen=True)
class RoleAssessment:
    role: CodeRole
    factor: float
    path: list[str] = field(default_factory=list)
    path_in_loop: bool = False


def _bfs(graph: nx.DiGraph, sources: Iterable[str], max_depth: int) -> dict[str, str | None]:
    """Multi-source BFS: node -> predecessor on a shortest path from the nearest source."""
    parents: dict[str, str | None] = {}
    queue: deque[tuple[str, int]] = deque()
    for source in sorted(sources):
        if source in graph and source not in parents:
            parents[source] = None
            queue.append((source, 0))
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for succ in sorted(graph.successors(node)):
            if succ not in parents:
                parents[succ] = node
                queue.append((succ, depth + 1))
    return parents


def _trace(parents: Mapping[str, str | None], node: str) -> list[str]:
    path = [node]
    current = parents.get(node)
    while current is not None:
        path.append(current)
        current = parents.get(current)
    return path[::-1]


def _loop_call_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Simple names of everything ``func`` calls once per iteration of one of its own loops.

    A ``for`` iterable and a comprehension's first iterable are evaluated once, so calls there do
    not count; nested functions and lambdas are not followed."""
    names: set[str] = set()

    def visit(node: ast.AST, in_loop: bool) -> None:
        if isinstance(node, ScopeNode):
            return
        if in_loop and isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
        if isinstance(node, ast.For | ast.AsyncFor):
            visit(node.iter, in_loop)
            for stmt in node.body:
                visit(stmt, True)
            for stmt in node.orelse:
                visit(stmt, in_loop)
            return
        if isinstance(node, ast.While):
            visit(node.test, True)
            for stmt in node.body:
                visit(stmt, True)
            for stmt in node.orelse:
                visit(stmt, in_loop)
            return
        if isinstance(node, COMPREHENSIONS):
            first, *rest = node.generators
            visit(first.iter, in_loop)
            for condition in first.ifs:
                visit(condition, True)
            for generator in rest:
                visit(generator, True)
            for name in ("elt", "key", "value"):
                if isinstance(part := getattr(node, name, None), ast.AST):
                    visit(part, True)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_loop)

    for stmt in func.body:
        visit(stmt, False)
    return names


class PathRanker:
    """Classifies code by execution role using the code index and its graphs."""

    def __init__(
        self,
        index: CodeIndex,
        import_graph: nx.DiGraph,
        call_graph: nx.DiGraph,
        parsed: Mapping[str, ParsedModule] | None = None,
        max_depth: int = MAX_PATH_DEPTH,
    ) -> None:
        self.call_graph = call_graph
        self.parsed = parsed or {}
        self.file_by_module = {m.module_name: m.file_path for m in index.modules}
        self.entry_roles: dict[str, CodeRole] = {}
        handler_modules: set[str] = set()
        for module in index.modules:
            if path_hint(module.file_path) in _DECISIVE_HINTS:
                continue
            reasons = _symbol_reasons(module)
            for symbol in module.symbols:
                role = entry_role(module, symbol, reasons)
                if role is None:
                    continue
                self.entry_roles[symbol_id(module.module_name, symbol.qualname)] = role
                if role in {CodeRole.REQUEST_HANDLER, CodeRole.BACKGROUND_TASK}:
                    handler_modules.add(module.module_name)

        def sources(role: CodeRole) -> list[str]:
            return [s for s, r in self.entry_roles.items() if r is role]

        self.request_reach = _bfs(call_graph, sources(CodeRole.REQUEST_HANDLER), max_depth)
        self.background_reach = _bfs(call_graph, sources(CodeRole.BACKGROUND_TASK), max_depth)
        self.cli_reach = _bfs(call_graph, sources(CodeRole.CLI_SCRIPT), max_depth)
        self.import_reach = _bfs(import_graph, handler_modules, max_depth)
        self.service_entrypoints = bool(handler_modules)
        self._loop_calls: dict[str, set[str]] = {}

    def factor(self, role: CodeRole) -> float:
        if role is CodeRole.CLI_SCRIPT and not self.service_entrypoints:
            return CLI_PRIMARY_FACTOR
        return ROLE_FACTORS[role]

    def _node(self, module_name: str, symbol: str | None) -> str | None:
        """Call-graph node of the symbol, or of its nearest enclosing function."""
        if not symbol:
            return None
        parts = symbol.split(".")
        for depth in range(len(parts), 0, -1):
            candidate = symbol_id(module_name, ".".join(parts[:depth]))
            if candidate in self.call_graph:
                return candidate
        return None

    def assess(self, module_name: str, file_path: str, symbol: str | None) -> RoleAssessment:
        node = self._node(module_name, symbol)
        hint = path_hint(file_path)
        if hint in _DECISIVE_HINTS:
            assert hint is not None
            return self._assessment(hint, [node] if node else [])
        if node is not None:
            role = self.entry_roles.get(node)
            if role in {CodeRole.REQUEST_HANDLER, CodeRole.BACKGROUND_TASK}:
                assert role is not None
                return self._assessment(role, [node])
            for reach, reached_role in (
                (self.request_reach, CodeRole.REQUEST_PATH),
                (self.background_reach, CodeRole.BACKGROUND_TASK),
            ):
                if node in reach:
                    return self._assessment(reached_role, _trace(reach, node))
        if hint is CodeRole.BACKGROUND_TASK:
            return self._assessment(hint, [node] if node else [])
        if node is not None and node in self.cli_reach:
            return self._assessment(CodeRole.CLI_SCRIPT, _trace(self.cli_reach, node))
        if module_name in self.import_reach:
            path = _trace(self.import_reach, module_name)
            if node is not None:
                path[-1] = node
            return self._assessment(CodeRole.LIBRARY, path)
        return self._assessment(CodeRole.INTERNAL, [node] if node else [])

    def _assessment(self, role: CodeRole, path: list[str]) -> RoleAssessment:
        in_loop = any(self._called_in_loop(a, b) for a, b in itertools.pairwise(path))
        return RoleAssessment(role, self.factor(role), path, in_loop)

    def _called_in_loop(self, caller: str, callee: str) -> bool:
        if ":" not in caller or ":" not in callee:
            return False  # import-graph hops carry no call site
        names = self._loop_calls.get(caller)
        if names is None:
            names = self._loop_calls[caller] = self._caller_loop_names(caller)
        qualname = callee.partition(":")[2].split(".")
        wanted = {qualname[-1]}
        if qualname[-1] == "__init__" and len(qualname) > 1:
            wanted.add(qualname[-2])
        return bool(names & wanted)

    def _caller_loop_names(self, caller: str) -> set[str]:
        module_name, _, qualname = caller.partition(":")
        file_path = self.file_by_module.get(module_name)
        module = self.parsed.get(file_path) if file_path else None
        if module is None or module.tree is None:
            return set()
        func = _find_function(module.tree.body, qualname.split("."))
        return _loop_call_names(func) if func is not None else set()


def _find_function(
    body: list[ast.stmt], parts: list[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and (
            node.name == parts[0]
        ):
            if len(parts) == 1:
                return None if isinstance(node, ast.ClassDef) else node
            return _find_function(node.body, parts[1:])
        if isinstance(node, ast.If | ast.Try | ast.With):
            nested = [*node.body, *getattr(node, "orelse", []), *getattr(node, "finalbody", [])]
            for handler in getattr(node, "handlers", []):
                nested.extend(handler.body)
            found = _find_function(nested, parts)
            if found is not None:
                return found
    return None
