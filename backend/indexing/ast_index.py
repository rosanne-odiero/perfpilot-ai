"""Symbol extraction: functions, methods and classes with spans and simple complexity facts."""

from __future__ import annotations

import ast
from collections.abc import Iterator

from backend.indexing.names import dotted_name
from backend.models.audit_schemas import SymbolInfo

MAX_CALLS_PER_SYMBOL = 200
MAX_DOCSTRING_CHARS = 500
MAX_DECORATOR_CHARS = 120

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
ScopeNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def iter_own_nodes(nodes: list[ast.stmt] | list[ast.AST]) -> Iterator[ast.AST]:
    """Walk nodes without descending into nested functions, lambdas, or classes."""
    stack: list[ast.AST] = list(reversed(nodes))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ScopeNode):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def max_loop_depth(nodes: list[ast.stmt]) -> int:
    def depth(node: ast.AST) -> int:
        if isinstance(node, ScopeNode):
            return 0
        own = 0
        if isinstance(node, LOOP_NODES):
            own = 1
        elif isinstance(node, COMPREHENSIONS):
            own = len(node.generators)
        children = [depth(child) for child in ast.iter_child_nodes(node)]
        return own + max(children, default=0)

    return max((depth(n) for n in nodes), default=0)


def own_calls(node: FunctionNode) -> list[ast.Call]:
    return [n for n in iter_own_nodes(node.body) if isinstance(n, ast.Call)]


def self_call_count(
    node: FunctionNode, class_name: str | None, calls: list[ast.Call] | None = None
) -> int:
    """How many times the function calls itself directly (by name or via self/cls)."""
    count = 0
    for call in own_calls(node) if calls is None else calls:
        func = call.func
        if (class_name is None and isinstance(func, ast.Name) and func.id == node.name) or (
            class_name is not None
            and isinstance(func, ast.Attribute)
            and func.attr == node.name
            and isinstance(func.value, ast.Name)
            and func.value.id in {"self", "cls", class_name}
        ):
            count += 1
    return count


def _symbol(
    node: FunctionNode | ast.ClassDef, qualname: str, path: str, class_name: str | None
) -> SymbolInfo:
    decorators = [ast.unparse(d)[:MAX_DECORATOR_CHARS] for d in node.decorator_list]
    docstring = ast.get_docstring(node)
    if docstring and len(docstring) > MAX_DOCSTRING_CHARS:
        docstring = docstring[:MAX_DOCSTRING_CHARS] + "…"
    line_start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    line_end = node.end_lineno or node.lineno
    if isinstance(node, ast.ClassDef):
        return SymbolInfo(
            qualname=qualname,
            kind="class",
            file_path=path,
            line_start=line_start,
            line_end=line_end,
            decorators=decorators,
            docstring=docstring,
        )
    is_async = isinstance(node, ast.AsyncFunctionDef)
    if class_name is not None:
        kind = "async_method" if is_async else "method"
    else:
        kind = "async_function" if is_async else "function"
    call_nodes = own_calls(node)
    calls = sorted({name for c in call_nodes if (name := dotted_name(c.func))})
    return SymbolInfo(
        qualname=qualname,
        kind=kind,
        file_path=path,
        line_start=line_start,
        line_end=line_end,
        decorators=decorators,
        max_loop_depth=max_loop_depth(node.body),
        is_recursive=self_call_count(node, class_name, call_nodes) > 0,
        calls=calls[:MAX_CALLS_PER_SYMBOL],
        docstring=docstring,
    )


def extract_symbols(path: str, tree: ast.Module) -> list[SymbolInfo]:
    symbols: list[SymbolInfo] = []

    def visit(body: list[ast.stmt], stack: list[str], class_name: str | None) -> None:
        for node in iter_own_nodes(body):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = ".".join([*stack, node.name])
                symbols.append(_symbol(node, qualname, path, class_name))
                visit(node.body, [*stack, node.name], None)
            elif isinstance(node, ast.ClassDef):
                qualname = ".".join([*stack, node.name])
                symbols.append(_symbol(node, qualname, path, None))
                visit(node.body, [*stack, node.name], node.name)

    visit(tree.body, [], None)
    return sorted(symbols, key=lambda s: (s.line_start, s.qualname))
