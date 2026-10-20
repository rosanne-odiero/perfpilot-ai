"""Best-effort, name-based intra-repository call graph.

Nodes are ``"<module>:<qualname>"``. A call is linked only when the callee can be resolved by name:
a function defined in the same module or enclosing function, a method called through
``self``/``cls``/the class name, a name imported from an internal module, or ``module.func`` where
``module`` is an imported internal module. Dynamic dispatch, decorators that replace functions, and
attribute access on instances are not followed, so the graph under-approximates real calls.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

import networkx as nx

from backend.indexing.ast_index import iter_own_nodes
from backend.indexing.module_resolution import ModuleNaming
from backend.indexing.source import ParsedModule


def symbol_id(module_name: str, qualname: str) -> str:
    return f"{module_name}:{qualname}"


@dataclass
class _ModuleScope:
    module: str
    defs: set[str] = field(default_factory=set)
    module_aliases: dict[str, str] = field(default_factory=dict)
    symbol_aliases: dict[str, str] = field(default_factory=dict)


def _bindings(parsed: ParsedModule, naming: ModuleNaming, scope: _ModuleScope) -> None:
    for local, origin in parsed.aliases.names.items():
        resolved = naming.resolve_origin(parsed.path, origin)
        if resolved is None:
            continue
        module_name, attr = resolved
        if attr is None:
            scope.module_aliases[local] = module_name
        else:
            scope.symbol_aliases[local] = symbol_id(module_name, attr)


_FunctionEntry = tuple[ParsedModule, ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]


def _collect_defs(
    module: ParsedModule,
    scope: _ModuleScope,
    body: list[ast.stmt],
    stack: list[str],
    cls: str | None,
    functions: list[_FunctionEntry],
) -> None:
    for node in iter_own_nodes(body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = ".".join([*stack, node.name])
            scope.defs.add(qualname)
            functions.append((module, node, qualname, cls))
            _collect_defs(module, scope, node.body, [*stack, node.name], None, functions)
        elif isinstance(node, ast.ClassDef):
            qualname = ".".join([*stack, node.name])
            scope.defs.add(qualname)
            _collect_defs(module, scope, node.body, [*stack, node.name], qualname, functions)


def build_call_graph(parsed: dict[str, ParsedModule], naming: ModuleNaming) -> nx.DiGraph:
    graph = nx.DiGraph()
    scopes: dict[str, _ModuleScope] = {}
    functions: list[_FunctionEntry] = []
    for module in parsed.values():
        if module.tree is None:
            continue
        scope = _ModuleScope(module.module_name)
        scopes[module.path] = scope
        _collect_defs(module, scope, module.tree.body, [], None, functions)
        graph.add_nodes_from(symbol_id(scope.module, q) for q in sorted(scope.defs))
        _bindings(module, naming, scope)

    known = set(graph.nodes)
    for module, node, qualname, cls in functions:
        scope = scopes[module.path]
        caller = symbol_id(scope.module, qualname)
        parents = qualname.split(".")
        for call in (n for n in iter_own_nodes(node.body) if isinstance(n, ast.Call)):
            callee = _resolve(call.func, scope, parents, cls)
            if callee and f"{callee}.__init__" in known:
                callee = f"{callee}.__init__"
            if callee and callee in known and callee != caller:
                graph.add_edge(caller, callee)
    return graph


def _resolve(
    func: ast.expr, scope: _ModuleScope, parents: list[str], cls: str | None
) -> str | None:
    if isinstance(func, ast.Name):
        for depth in range(len(parents), -1, -1):
            candidate = ".".join([*parents[:depth], func.id])
            if candidate in scope.defs:
                init = f"{candidate}.__init__"
                return symbol_id(scope.module, init if init in scope.defs else candidate)
        return scope.symbol_aliases.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = func.value.id
        if cls and owner in {"self", "cls", cls.rsplit(".", 1)[-1]}:
            return symbol_id(scope.module, f"{cls}.{func.attr}")
        if owner in scope.defs:
            return symbol_id(scope.module, f"{owner}.{func.attr}")
        if owner in scope.module_aliases:
            return symbol_id(scope.module_aliases[owner], func.attr)
    return None
