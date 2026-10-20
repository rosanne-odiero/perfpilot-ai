"""Deterministic, AST-guided source transformations for the bundled rule families.

Each transform receives a :class:`TransformContext` (the parsed target file, the target function
and the flagged line span) and either returns text edits or raises :class:`NoSafePatch` with the
reason it refuses. A transform only fires when it can show that the rewrite keeps behavior for
well-formed inputs; remaining semantic risks are returned as notes and repeated in the
recommendation. Formatting and comments outside the edited spans are preserved.
"""

from __future__ import annotations

import ast
import keyword
import re
import symtable
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from backend.optimization.source_edits import Edits, SourceText

LoopNode = ast.For | ast.While
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_SEQUENCE_TYPES = {"list", "List", "tuple", "Tuple"}
_SEQUENCE_FACTORIES = {"list", "tuple", "sorted"}
_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]+")


class NoSafePatch(Exception):
    """The transformation cannot be proven safe for this code; nothing is guessed."""


@dataclass
class TransformResult:
    name: str
    edits: Edits
    summary: str
    notes: list[str] = field(default_factory=list)


@dataclass
class TransformContext:
    path: str
    source: SourceText
    tree: ast.Module
    function: ast.FunctionDef | ast.AsyncFunctionDef
    qualname: str
    line_start: int
    line_end: int
    scope: symtable.SymbolTable
    parents: dict[ast.AST, ast.AST]
    taken_names: set[str]

    # -- generic queries -------------------------------------------------------------------

    def in_span(self, node: ast.AST) -> bool:
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        return (
            start is not None
            and end is not None
            and start <= self.line_end
            and end >= self.line_start
        )

    def nodes_in_span(self, predicate: Callable[[ast.AST], bool]) -> list[ast.AST]:
        found = [n for n in ast.walk(self.function) if self.in_span(n) and predicate(n)]
        return sorted(found, key=lambda n: (n.lineno, n.col_offset))  # type: ignore[attr-defined]

    def unique_name(self, base: str) -> str:
        cleaned = _IDENTIFIER.sub("_", base) or "value"
        if cleaned[0].isdigit() or keyword.iskeyword(cleaned):
            cleaned = f"_{cleaned}"
        name, counter = cleaned, 2
        while name in self.taken_names:
            name, counter = f"{cleaned}_{counter}", counter + 1
        self.taken_names.add(name)
        return name

    def indent(self, lineno: int) -> str:
        indent = self.source.indent(lineno)
        if "\t" in indent:
            raise NoSafePatch("tab indentation is not supported by the deterministic patcher")
        return indent

    def is_local(self, name: str) -> bool:
        try:
            symbol = self.scope.lookup(name)
        except KeyError:
            return False
        return symbol.is_parameter() or (
            symbol.is_local() and not symbol.is_global() and not symbol.is_nonlocal()
        )

    def is_module_global(self, name: str) -> bool:
        try:
            symbol = self.scope.lookup(name)
        except KeyError:
            return True  # not referenced in the function at all: no shadowing
        return not symbol.is_local() and not symbol.is_parameter() and not symbol.is_free()

    def enclosing_loops(self, node: ast.AST) -> list[LoopNode]:
        """Loops (innermost first) whose per-iteration code contains ``node``, within the target
        function. Stops at nested scopes and comprehensions."""
        loops: list[LoopNode] = []
        child = node
        parent = self.parents.get(child)
        while parent is not None and parent is not self.function:
            if isinstance(parent, (*_SCOPES, ast.comprehension)):
                raise NoSafePatch("the flagged code is inside a nested scope or comprehension")
            if isinstance(parent, LoopNode) and child in loop_inside(parent):
                loops.append(parent)
            child, parent = parent, self.parents.get(parent)
        return loops

    def ancestors(self, node: ast.AST) -> Iterable[ast.AST]:
        parent = self.parents.get(node)
        while parent is not None and parent is not self.function:
            yield parent
            parent = self.parents.get(parent)

    def containing_block(self, stmt: ast.stmt) -> tuple[list[ast.stmt], int]:
        parent = self.parents[stmt]
        for field_name in ("body", "orelse", "finalbody", "handlers"):
            block = getattr(parent, field_name, None)
            if isinstance(block, list) and any(item is stmt for item in block):
                return block, next(i for i, item in enumerate(block) if item is stmt)
        raise NoSafePatch("could not locate the statement's enclosing block")

    def require_sequence(self, name: str) -> None:
        """``name`` must be a parameter/local whose binding is provably a list or tuple, because
        ``in``/``.count`` mean something different on ``str`` (substrings) and ``dict`` (keys)."""
        if not self.is_local(name):
            raise NoSafePatch(
                f"`{name}` is not a parameter or local variable of `{self.qualname}`; another "
                "function could change it between loop iterations"
            )
        args = self.function.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        param = next((arg for arg in params if arg.arg == name), None)
        if param is not None and (
            param.annotation is None or not _is_sequence_annotation(param.annotation)
        ):
            raise NoSafePatch(
                f"cannot prove parameter `{name}` is a list or tuple (it needs a list/tuple "
                "annotation); `in` and `.count` behave differently on str and dict"
            )
        bindings = _sequence_bindings(self, name)
        if bindings is None or (param is None and bindings == 0):
            raise NoSafePatch(
                f"cannot prove `{name}` is always a list or tuple from its assignments"
            )


def loop_inside(loop: LoopNode) -> list[ast.AST]:
    """Nodes evaluated once per iteration (a ``for`` header's iterable is evaluated once)."""
    nodes: list[ast.AST] = [*loop.body, *loop.orelse]
    if isinstance(loop, ast.While):
        nodes.insert(0, loop.test)
    return nodes


def occurrences(nodes: Iterable[ast.AST], name: str) -> list[ast.Name]:
    return [n for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == name]


def _annotation_node(node: ast.expr) -> ast.expr | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    return node


def _is_sequence_annotation(annotation: ast.expr) -> bool:
    node = _annotation_node(annotation)
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id in _SEQUENCE_TYPES
    if isinstance(node, ast.Attribute):
        return node.attr in _SEQUENCE_TYPES and isinstance(node.value, ast.Name)
    return False


def _sequence_bindings(context: TransformContext, name: str) -> int | None:
    """Number of assignments to ``name`` in the function, or ``None`` if any of them is not
    provably a list/tuple (or ``name`` is bound by import, def, except, with, for, ...)."""
    count = 0
    for node in ast.walk(context.function):
        if isinstance(node, ast.alias) and (node.asname or node.name) == name:
            return None
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node is not context.function
            and node.name == name
        ):
            return None
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return None
        if not (isinstance(node, ast.Name) and node.id == name):
            continue
        if isinstance(node.ctx, ast.Load):
            continue
        parent = context.parents.get(node)
        if isinstance(parent, ast.Assign) and any(t is node for t in parent.targets):
            if not _value_is_sequence(parent.value):
                return None
        elif isinstance(parent, ast.AnnAssign) and parent.target is node:
            if parent.value is not None and not _value_is_sequence(parent.value):
                return None
        elif not (isinstance(parent, ast.AugAssign) and parent.target is node):
            return None
        count += 1
    return count


def _value_is_sequence(value: ast.expr | None) -> bool:
    if isinstance(value, ast.List | ast.ListComp | ast.Tuple):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _SEQUENCE_FACTORIES
    )


def build_context(
    path: str,
    text: str,
    tree: ast.Module,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    qualname: str,
    line_start: int,
    line_end: int,
) -> TransformContext:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    scope = _function_scope(symtable.symtable(text, path, "exec"), function)
    taken = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    taken |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    taken |= {
        alias.asname or alias.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for alias in n.names
    }
    taken |= {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    context = TransformContext(
        path,
        SourceText(text),
        tree,
        function,
        qualname,
        line_start,
        line_end,
        scope,
        parents,
        taken,
    )
    _reject_shared_bindings(context)
    return context


def _reject_shared_bindings(context: TransformContext) -> None:
    for node in ast.walk(context.function):
        if isinstance(node, ast.Global | ast.Nonlocal):
            raise NoSafePatch(
                f"`{context.qualname}` uses global/nonlocal declarations; the deterministic "
                "patcher does not rewrite functions with shared mutable bindings"
            )


def _function_scope(table: symtable.SymbolTable, function: ast.AST) -> symtable.SymbolTable:
    name = function.name  # type: ignore[attr-defined]
    lineno = function.lineno  # type: ignore[attr-defined]
    stack = [table]
    while stack:
        current = stack.pop()
        if (
            current.get_type() == "function"
            and current.get_name() == name
            and current.get_lineno() == lineno
        ):
            return current
        stack.extend(current.get_children())
    raise NoSafePatch("could not resolve the function's symbol table")


# ------------------------------------------------------------------------------------------
# Shared rewrite helpers
# ------------------------------------------------------------------------------------------


def _hoist_loop(
    context: TransformContext, loops: Sequence[LoopNode], name: str, ok: Callable[[ast.Name], bool]
) -> LoopNode:
    """Outermost enclosing loop in which every use of ``name`` satisfies ``ok``."""
    hoist: LoopNode | None = None
    for loop in loops:
        if all(ok(occurrence) for occurrence in occurrences(loop_inside(loop), name)):
            hoist = loop
        else:
            break
    if hoist is None:
        raise NoSafePatch(
            f"`{name}` is used inside the loop in other ways (it may be modified or aliased), so "
            "it cannot be indexed once before the loop"
        )
    return hoist


def _import_anchor(context: TransformContext) -> tuple[ast.stmt | None, bool]:
    """The last top-level import before the target function (and whether it is the
    ``__future__`` import), or the module docstring when there are no imports."""
    anchor: ast.stmt | None = None
    future = False
    for stmt in context.tree.body:
        if stmt.lineno >= context.function.lineno:
            break
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            anchor = stmt
            future = isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"
    if anchor is None and context.tree.body:
        first = context.tree.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            return first, True
    return anchor, future


def _module_binding(context: TransformContext, module: str, name: str) -> str | None:
    """How ``module.name`` is already reachable at module level, if it is."""
    for stmt in context.tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == module and stmt.level == 0:
            for alias in stmt.names:
                if alias.name == name:
                    return alias.asname or alias.name
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == module:
                    return f"{alias.asname or alias.name}.{name}"
    return None


def _ensure_import(context: TransformContext, edits: Edits, module: str, name: str) -> str:
    reference = _module_binding(context, module, name)
    if reference is not None:
        root = reference.split(".")[0]
        if not context.is_module_global(root):
            raise NoSafePatch(f"`{root}` is shadowed inside `{context.qualname}`")
        return reference
    if name in context.taken_names:
        raise NoSafePatch(f"the name `{name}` is already used in this module for something else")
    context.taken_names.add(name)
    statement = f"from {module} import {name}"
    anchor, separate = _import_anchor(context)
    if anchor is None:
        edits.insert(0, f"{statement}\n\n")
    else:
        end_line = anchor.end_lineno or anchor.lineno
        gap = "\n\n" if separate else "\n"
        edits.insert(context.source.line_end(end_line), f"{gap}{statement}")
    return name


def _insert_before(
    context: TransformContext, edits: Edits, stmt: ast.stmt, lines: list[str]
) -> None:
    indent = context.indent(stmt.lineno)
    block = "".join(f"{indent}{line}\n" for line in lines)
    edits.insert(context.source.line_start(stmt.lineno), block)


def _no_multiline_strings(nodes: Iterable[ast.AST]) -> None:
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant | ast.JoinedStr) and child.lineno != child.end_lineno:
                raise NoSafePatch("re-indenting code that contains multi-line strings is unsafe")


# ------------------------------------------------------------------------------------------
# PP-LOOP-001: membership test against a list inside a loop -> set built once
# ------------------------------------------------------------------------------------------


def _is_membership_operand(context: TransformContext, node: ast.Name) -> bool:
    parent = context.parents.get(node)
    return (
        isinstance(parent, ast.Compare)
        and len(parent.ops) == 1
        and isinstance(parent.ops[0], ast.In | ast.NotIn)
        and parent.comparators[0] is node
    )


def membership_to_set(context: TransformContext) -> TransformResult:
    compares = context.nodes_in_span(
        lambda n: (
            isinstance(n, ast.Compare)
            and len(n.ops) == 1
            and isinstance(n.ops[0], ast.In | ast.NotIn)
            and isinstance(n.comparators[0], ast.Name)
        )
    )
    if not compares:
        raise NoSafePatch(
            "no `x in <name>` membership test against a plain name at the flagged lines"
        )
    first = compares[0]
    assert isinstance(first, ast.Compare)
    operand = first.comparators[0]
    assert isinstance(operand, ast.Name)
    name = operand.id
    loops = context.enclosing_loops(first)
    if not loops:
        raise NoSafePatch("the membership test is not inside a loop")
    context.require_sequence(name)
    hoist = _hoist_loop(context, loops, name, lambda n: _is_membership_operand(context, n))
    set_name = context.unique_name(f"{name}_set")
    edits = Edits()
    _insert_before(context, edits, hoist, [f"{set_name} = set({name})"])
    for occurrence in occurrences(loop_inside(hoist), name):
        edits.replace(context.source.start(occurrence), context.source.end(occurrence), set_name)
    return TransformResult(
        name="list_membership_to_set",
        edits=edits,
        summary=(
            f"Build `{set_name} = set({name})` once before the loop at line {hoist.lineno} and "
            f"test membership against the set (O(1) average) instead of scanning the list."
        ),
        notes=[
            f"Elements of `{name}` must be hashable; otherwise `set()` raises TypeError.",
            "Set membership uses hash + ==; types whose __hash__ disagrees with __eq__ behave "
            "differently.",
            f"`set({name})` is evaluated even when the loop body never runs, so an invalid "
            f"`{name}` can raise earlier than before.",
            "Extra memory: one set of the list's elements.",
        ],
    )


# ------------------------------------------------------------------------------------------
# PP-COLL-001: list.count() inside a loop -> collections.Counter built once
# ------------------------------------------------------------------------------------------


def _count_call(context: TransformContext, node: ast.Name) -> ast.Call | None:
    attribute = context.parents.get(node)
    call = context.parents.get(attribute) if attribute is not None else None
    if (
        isinstance(attribute, ast.Attribute)
        and attribute.attr == "count"
        and isinstance(call, ast.Call)
        and call.func is attribute
        and len(call.args) == 1
        and not isinstance(call.args[0], ast.Starred)
        and not call.keywords
    ):
        return call
    return None


def list_count_to_counter(context: TransformContext) -> TransformResult:
    calls = context.nodes_in_span(
        lambda n: (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "count"
            and isinstance(n.func.value, ast.Name)
        )
    )
    if not calls:
        raise NoSafePatch(
            "no `<name>.count(x)` call at the flagged lines (only .count is rewritten)"
        )
    first = calls[0]
    assert isinstance(first, ast.Call)
    assert isinstance(first.func, ast.Attribute)
    assert isinstance(first.func.value, ast.Name)
    name = first.func.value.id
    loops = context.enclosing_loops(first)
    if not loops:
        raise NoSafePatch("the .count() call is not inside a loop")
    context.require_sequence(name)
    hoist = _hoist_loop(context, loops, name, lambda n: _count_call(context, n) is not None)
    edits = Edits()
    counter = _ensure_import(context, edits, "collections", "Counter")
    counts_name = context.unique_name(f"{name}_counts")
    _insert_before(context, edits, hoist, [f"{counts_name} = {counter}({name})"])
    for occurrence in occurrences(loop_inside(hoist), name):
        call = _count_call(context, occurrence)
        assert call is not None
        argument = context.source.segment(call.args[0])
        edits.replace(
            context.source.start(call), context.source.end(call), f"{counts_name}[{argument}]"
        )
    return TransformResult(
        name="list_count_to_counter",
        edits=edits,
        summary=(
            f"Count every element of `{name}` once with `{counter}({name})` before the loop at "
            f"line {hoist.lineno} and read counts from it (O(n) total instead of O(n) per call)."
        ),
        notes=[
            f"Elements of `{name}` (and the values counted) must be hashable; otherwise "
            "Counter raises TypeError where list.count() worked.",
            "Counter matches by hash + ==, list.count() by == only; they agree for "
            "well-behaved types.",
            "A missing key reads as 0, exactly like list.count() for an absent value.",
            "Extra memory: one dict entry per distinct element.",
        ],
    )


# ------------------------------------------------------------------------------------------
# PP-STR-001: str += in a loop -> list of parts + "".join
# ------------------------------------------------------------------------------------------


def _concat_target(node: ast.AST) -> str | None:
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        name = node.target.id
    elif isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        name = target.id if isinstance(target, ast.Name) else ""
    else:
        return None
    return name if name and _concat_statement(node, name) is not None else None


def _is_concat_operand(statement: ast.AST, occurrence: ast.Name) -> bool:
    if isinstance(statement, ast.AugAssign):
        return statement.target is occurrence
    if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.BinOp):
        return occurrence is statement.targets[0] or occurrence is statement.value.left
    return False


def _concat_statement(node: ast.AST, name: str) -> ast.expr | None:
    """The appended expression if ``node`` is ``name += expr`` or ``name = name + expr``."""
    if (
        isinstance(node, ast.AugAssign)
        and isinstance(node.op, ast.Add)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
    ):
        return node.value
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Add)
        and isinstance(node.value.left, ast.Name)
        and node.value.left.id == name
    ):
        return node.value.right
    return None


def _string_init(stmt: ast.stmt, name: str) -> ast.Constant | None:
    target: ast.expr | None = None
    value: ast.expr | None = None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target, value = stmt.targets[0], stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.simple:
        target, value = stmt.target, stmt.value
    if (
        isinstance(target, ast.Name)
        and target.id == name
        and isinstance(value, ast.Constant)
        and isinstance(value.value, str)
    ):
        return value
    return None


def string_concat_to_join(context: TransformContext) -> TransformResult:
    statements = context.nodes_in_span(lambda n: _concat_target(n) is not None)
    if not statements:
        raise NoSafePatch("no `s += ...` string concatenation at the flagged lines")
    first = statements[0]
    name = _concat_target(first)
    assert name is not None
    loops = context.enclosing_loops(first)
    if not loops:
        raise NoSafePatch("the concatenation is not inside a loop")
    loop, init = _find_string_loop(context, loops, name)
    for ancestor in [loop, *context.ancestors(loop)]:
        if isinstance(ancestor, ast.Try | ast.TryStar | ast.With | ast.AsyncWith):
            raise NoSafePatch(
                "the loop runs inside try/with; if an exception is handled mid-loop the "
                "original keeps the partial string, which the rewrite would not"
            )
    appends: list[tuple[ast.stmt, ast.expr]] = []
    for occurrence in occurrences(loop_inside(loop), name):
        statement = context.parents.get(occurrence)
        if isinstance(statement, ast.BinOp):
            statement = context.parents.get(statement)
        expression = _concat_statement(statement, name) if statement is not None else None
        if statement is None or expression is None or not _is_concat_operand(statement, occurrence):
            raise NoSafePatch(
                f"`{name}` is read or rebound inside the loop other than by `{name} += ...`"
            )
        assert isinstance(statement, ast.stmt)
        if all(existing is not statement for existing, _ in appends):
            appends.append((statement, expression))
    parts = context.unique_name(f"{name}_parts")
    literal = context.source.segment(init)
    initial = "[]" if init.value == "" else f"[{literal}]"
    init_stmt = context.parents[init]
    assert isinstance(init_stmt, ast.stmt)
    annotation = ""
    if isinstance(init_stmt, ast.AnnAssign):
        annotation = f": {context.source.segment(init_stmt.annotation)}"
    edits = Edits()
    edits.replace(
        context.source.start(init_stmt),
        context.source.end(init_stmt),
        f"{parts}: list[str] = {initial}",
    )
    for statement, expression in appends:
        edits.replace(
            context.source.start(statement),
            context.source.end(statement),
            f"{parts}.append({context.source.segment(expression)})",
        )
    indent = context.indent(loop.lineno)
    end_line = loop.end_lineno or loop.lineno
    edits.insert(
        context.source.line_end(end_line), f'\n{indent}{name}{annotation} = "".join({parts})'
    )
    return TransformResult(
        name="string_concat_to_join",
        edits=edits,
        summary=(
            f"Collect the pieces of `{name}` in the list `{parts}` inside the loop at line "
            f'{loop.lineno} and build the string once with `"".join({parts})` after it.'
        ),
        notes=[
            "CPython often grows a uniquely referenced string in place, so `+=` may already "
            "be linear; the benefit must be measured and can be zero or negative.",
            "Peak memory can rise: all parts are alive until the join.",
            "A non-str piece now raises TypeError at the join instead of at the `+=`.",
        ],
    )


def _find_string_loop(
    context: TransformContext, loops: Sequence[LoopNode], name: str
) -> tuple[LoopNode, ast.Constant]:
    """The loop to rewrite and the literal initialising ``name`` before it. The search walks up
    through enclosing blocks (``if``/``try``/``with``) until the next enclosing loop."""
    for position, loop in enumerate(loops):
        boundary = loops[position + 1] if position + 1 < len(loops) else context.function
        statement: ast.AST = loop
        while statement is not boundary and statement is not context.function:
            if isinstance(statement, ast.stmt):
                block, index = context.containing_block(statement)
                for previous in reversed(block[:index]):
                    if occurrences([previous], name):
                        init = _string_init(previous, name)
                        if init is None:
                            raise NoSafePatch(
                                f"`{name}` is used before the loop in a way other than a string "
                                "literal initialisation"
                            )
                        return loop, init
            statement = context.parents[statement]
    raise NoSafePatch(f"`{name}` is not initialised to a string literal before the loop")


# ------------------------------------------------------------------------------------------
# PP-REGEX-001: re.<fn>(literal, ...) inside a loop -> module-level compiled pattern
# ------------------------------------------------------------------------------------------

# function -> number of positional arguments (after the pattern) that precede ``flags``
_RE_FLAGS_POSITION = {
    "search": 1,
    "match": 1,
    "fullmatch": 1,
    "findall": 1,
    "finditer": 1,
    "split": 2,
    "sub": 3,
    "subn": 3,
}
_RE_FLAG_NAMES = {
    "A", "ASCII", "DEBUG", "I", "IGNORECASE", "L", "LOCALE", "M", "MULTILINE", "NOFLAG",
    "S", "DOTALL", "U", "UNICODE", "X", "VERBOSE",
}  # fmt: skip


def _re_alias(context: TransformContext) -> set[str]:
    aliases = set()
    for stmt in context.tree.body:
        if isinstance(stmt, ast.Import):
            aliases |= {alias.asname or alias.name for alias in stmt.names if alias.name == "re"}
    return {alias for alias in aliases if context.is_module_global(alias)}


def _static_flags(node: ast.expr, aliases: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and node.attr in _RE_FLAG_NAMES
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _static_flags(node.left, aliases) and _static_flags(node.right, aliases)
    return False


def regex_compile_hoist(context: TransformContext) -> TransformResult:
    aliases = _re_alias(context)
    if not aliases:
        raise NoSafePatch("the module does not `import re` at top level (or it is shadowed)")
    calls = context.nodes_in_span(
        lambda n: (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id in aliases
            and n.func.attr in _RE_FLAGS_POSITION
        )
    )
    if not calls:
        raise NoSafePatch("no `re.<function>(pattern, ...)` call at the flagged lines")
    edits = Edits()
    hoisted: dict[tuple[str, str | None], str] = {}
    definitions: list[str] = []
    for call in calls:
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        if not context.enclosing_loops(call):
            continue
        replacement, key = _rewrite_re_call(context, call, aliases)
        if key not in hoisted:
            constant = context.unique_name(f"_{context.function.name.upper()}_PATTERN")
            hoisted[key] = constant
            flags = f", {key[1]}" if key[1] else ""
            definitions.append(f"{constant} = {call.func.value.id}.compile({key[0]}{flags})")
        edits.replace(
            context.source.start(call),
            context.source.end(call),
            f"{hoisted[key]}.{call.func.attr}({replacement})",
        )
    if not definitions:
        raise NoSafePatch("the regex call is not inside a loop")
    anchor, _ = _import_anchor(context)
    if anchor is None or not isinstance(anchor, ast.Import | ast.ImportFrom):
        raise NoSafePatch("could not find the import block to place the compiled pattern after")
    end_line = anchor.end_lineno or anchor.lineno
    edits.insert(context.source.line_end(end_line), "\n\n" + "\n".join(definitions))
    return TransformResult(
        name="regex_compile_hoist",
        edits=edits,
        summary=(
            "Compile the constant pattern once at module level and call the compiled "
            "pattern's method inside the loop, skipping re's per-call cache lookup."
        ),
        notes=[
            "`re` already caches compiled patterns by string, so the saving is only the cache "
            "lookup and argument handling per call; it may be small or not measurable.",
            "Compilation moves to import time; an invalid pattern now fails at import.",
        ],
    )


def _rewrite_re_call(
    context: TransformContext, call: ast.Call, aliases: set[str]
) -> tuple[str, tuple[str, str | None]]:
    assert isinstance(call.func, ast.Attribute)
    if not call.args or any(isinstance(a, ast.Starred) for a in call.args):
        raise NoSafePatch("the pattern must be the first positional argument (no *args)")
    if any(k.arg is None or k.arg == "pattern" for k in call.keywords):
        raise NoSafePatch("**kwargs and pattern= keyword calls are not rewritten")
    pattern = call.args[0]
    if not (isinstance(pattern, ast.Constant) and isinstance(pattern.value, str | bytes)):
        raise NoSafePatch("the regex pattern is not a string literal")
    flags_position = _RE_FLAGS_POSITION[call.func.attr]
    rest = call.args[1:]
    flags: ast.expr | None = None
    if len(rest) > flags_position:
        flags = rest[flags_position]
        if len(rest) > flags_position + 1:
            raise NoSafePatch("unexpected extra positional arguments")
        rest = rest[:flags_position]
    keywords = []
    for keyword_arg in call.keywords:
        if keyword_arg.arg == "flags":
            flags = keyword_arg.value
        else:
            keywords.append(keyword_arg)
    if flags is not None and not _static_flags(flags, aliases):
        raise NoSafePatch("regex flags are not a constant expression")
    arguments = [context.source.segment(a) for a in rest]
    arguments += [f"{k.arg}={context.source.segment(k.value)}" for k in keywords]
    flags_text = context.source.segment(flags) if flags is not None else None
    return ", ".join(arguments), (context.source.segment(pattern), flags_text)


# ------------------------------------------------------------------------------------------
# PP-LOOP-002: nested-loop equality join -> dict index built once
# ------------------------------------------------------------------------------------------

_KEY_NODES = (ast.Name, ast.Attribute, ast.Subscript, ast.Constant, ast.Load)


def _pure_key(node: ast.expr) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, _KEY_NODES):
            return False
        if isinstance(child, ast.Subscript) and not isinstance(child.slice, ast.Constant):
            return False
    return True


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _key_label(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return str(node.slice.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    return "key"


def nested_loop_to_index(context: TransformContext) -> TransformResult:
    inner_loops = context.nodes_in_span(lambda n: isinstance(n, ast.For))
    inner = next((loop for loop in reversed(inner_loops) if context.enclosing_loops(loop)), None)
    if inner is None:
        raise NoSafePatch("no `for` loop nested inside another loop at the flagged lines")
    assert isinstance(inner, ast.For)
    outer = context.enclosing_loops(inner)[0]
    if not isinstance(outer, ast.For) or not any(stmt is inner for stmt in outer.body):
        raise NoSafePatch("the inner loop must be a direct statement of an outer `for` loop")
    if inner.orelse or len(inner.body) != 1 or not isinstance(inner.body[0], ast.If):
        raise NoSafePatch("the inner loop body must be exactly one `if` with no `else`")
    condition = inner.body[0]
    if condition.orelse:
        raise NoSafePatch("the join condition has an `else` branch")
    if not isinstance(inner.target, ast.Name) or not isinstance(inner.iter, ast.Name):
        raise NoSafePatch("the inner loop must be `for <name> in <name>:`")
    item, collection = inner.target.id, inner.iter.id
    test = condition.test
    if not (
        isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
    ):
        raise NoSafePatch("the join condition must be a single `==` comparison")
    left, right = test.left, test.comparators[0]
    if item in _names(left) and item not in _names(right):
        inner_key, outer_key = left, right
    elif item in _names(right) and item not in _names(left):
        inner_key, outer_key = right, left
    else:
        raise NoSafePatch("exactly one side of `==` must depend on the inner loop variable")
    if _names(inner_key) != {item} or not _pure_key(inner_key) or not _pure_key(outer_key):
        raise NoSafePatch("join keys must be plain names, attributes, or constant subscripts")
    context.require_sequence(collection)
    body_nodes: list[ast.AST] = list(condition.body)
    _check_join_body(context, body_nodes, item, _names(outer_key))
    uses = occurrences(loop_inside(outer), collection)
    if any(use is not inner.iter for use in uses):
        raise NoSafePatch(
            f"`{collection}` is used inside the outer loop other than as the join input"
        )
    for use in occurrences([context.function], item):
        if isinstance(use.ctx, ast.Load) and not _within(context, use, inner):
            raise NoSafePatch(f"`{item}` is read outside the inner loop")
    first_body = condition.body[0]
    header_end = test.end_lineno or condition.lineno
    if first_body.lineno <= header_end:
        raise NoSafePatch("the `if` body must start on its own line")
    _no_multiline_strings(body_nodes)
    if_indent = context.indent(condition.lineno)
    body_indent = context.indent(first_body.lineno)
    if not body_indent.startswith(if_indent) or len(body_indent) <= len(if_indent):
        raise NoSafePatch("unexpected indentation of the join body")
    dedent = len(body_indent) - len(if_indent)
    step = " " * dedent

    index = context.unique_name(f"{collection}_by_{_key_label(inner_key)}")
    inner_key_text = context.source.segment(inner_key)
    edits = Edits()
    _insert_before(
        context,
        edits,
        outer,
        [
            f"{index} = {{}}",
            f"for {item} in {collection}:",
            f"{step}{index}.setdefault({inner_key_text}, []).append({item})",
        ],
    )
    edits.replace(
        context.source.start(inner.iter),
        context.source.end(inner.iter),
        f"{index}.get({context.source.segment(outer_key)}, ())",
    )
    end_line = condition.end_lineno or condition.lineno
    edits.replace(
        context.source.line_start(condition.lineno), context.source.line_start(header_end + 1), ""
    )
    for lineno in range(header_end + 1, end_line + 1):
        line = context.source.lines[lineno - 1]
        if not line.strip():
            continue
        if not line.startswith(step):
            raise NoSafePatch("a line in the join body is less indented than expected")
        start = context.source.line_start(lineno)
        edits.replace(start, start + dedent, "")
    return TransformResult(
        name="nested_loop_join_to_index",
        edits=edits,
        summary=(
            f"Group `{collection}` by `{inner_key_text}` once into `{index}` before the outer "
            f"loop at line {outer.lineno}, then visit only the matching items for each outer "
            "item: O(n + m) instead of O(n * m). Matching items keep their original order."
        ),
        notes=[
            "Join keys must be hashable and their __hash__ must agree with __eq__.",
            f"`{collection}` is now read even when the outer loop has no items, and the outer "
            "key is evaluated once per outer item even when there are no inner items, so "
            "malformed records can raise where the original returned early.",
            "Matching items are visited in their original relative order, so per-key "
            "accumulation (including float sums) happens in the same sequence.",
            "Extra memory: the index holds references to every inner item.",
        ],
    )


def _within(context: TransformContext, node: ast.AST, container: ast.AST) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if current is container:
            return True
        current = context.parents.get(current)
    return False


def _check_join_body(
    context: TransformContext, body: list[ast.AST], item: str, outer_names: set[str]
) -> None:
    for node in body:
        for child in ast.walk(node):
            stores = not isinstance(getattr(child, "ctx", ast.Load()), ast.Load)
            if isinstance(child, ast.Name) and stores and child.id in outer_names:
                raise NoSafePatch(f"the join body rebinds `{child.id}`, which the key uses")
            if (
                isinstance(child, ast.Subscript | ast.Attribute)
                and stores
                and item in _names(child.value)
            ):
                raise NoSafePatch(f"the join body modifies `{item}`, which may change its key")
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and item in _names(child.func.value)
            ):
                raise NoSafePatch(f"the join body calls a method on `{item}`")


Transform = Callable[[TransformContext], TransformResult]

TRANSFORMS: dict[str, Transform] = {
    "PP-LOOP-001": membership_to_set,
    "PP-COLL-001": list_count_to_counter,
    "PP-STR-001": string_concat_to_join,
    "PP-REGEX-001": regex_compile_hoist,
    "PP-LOOP-002": nested_loop_to_index,
}
