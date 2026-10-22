"""Shared analysis for the deterministic transforms: the parsed target, loop structure, and the
evidence a transform needs before it may rewrite code (what a name is bound to, whether it can
change inside a loop, whether the loop can be suspended).

Every refusal raises :class:`NoSafePatch` with a reason written for the person reading the
candidate: what the transform expected, what it found, and where (``line N``).
"""

from __future__ import annotations

import ast
import keyword
import re
import symtable
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from backend.optimization.patch_validator import MAX_FILE_BYTES
from backend.optimization.source_edits import Edits, SourceText

LoopNode = ast.For | ast.AsyncFor | ast.While
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]+")
_MAX_REEXPORT_DEPTH = 3

SEQUENCE_TYPES = frozenset({"list", "List", "tuple", "Tuple"})
SEQUENCE_FACTORIES = frozenset({"list", "tuple", "sorted"})
# Collections that can be iterated again from the start (unlike iterators and generators).
REITERABLE_TYPES = SEQUENCE_TYPES | {
    "set", "Set", "frozenset", "FrozenSet", "dict", "Dict", "Sequence", "MutableSequence",
    "Mapping", "MutableMapping", "Collection", "AbstractSet", "MutableSet", "deque", "Deque",
    "OrderedDict", "defaultdict", "DefaultDict", "Counter", "str", "bytes",
}  # fmt: skip
REITERABLE_FACTORIES = SEQUENCE_FACTORIES | {"set", "frozenset", "dict"}


class NoSafePatch(Exception):
    """The transformation cannot be proven safe for this code; nothing is guessed."""


class NotApplicable(NoSafePatch):
    """The code shape a transform rewrites is not present at the flagged lines at all."""


@dataclass
class TransformResult:
    name: str
    edits: Edits
    summary: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CollectionKind:
    """What a proof of a name's type must establish."""

    label: str  # shown to the user, e.g. "a list or tuple"
    annotations: frozenset[str]
    factories: frozenset[str]
    literals: tuple[type[ast.AST], ...]


SEQUENCE = CollectionKind(
    "a list or tuple",
    SEQUENCE_TYPES,
    SEQUENCE_FACTORIES,
    (ast.List, ast.ListComp, ast.Tuple),
)
REITERABLE = CollectionKind(
    "a re-iterable collection (list, tuple, set, dict, str, ...; not an iterator or generator)",
    REITERABLE_TYPES,
    REITERABLE_FACTORIES,
    (ast.List, ast.ListComp, ast.Tuple, ast.Set, ast.SetComp, ast.Dict, ast.DictComp),
)


@dataclass
class TransformContext:
    path: str
    source: SourceText
    tree: ast.Module
    function: FunctionNode
    qualname: str
    line_start: int
    line_end: int
    scope: symtable.SymbolTable
    parents: dict[ast.AST, ast.AST]
    taken_names: set[str]
    source_root: Path | None = None
    _modules: dict[Path, ast.Module | None] = field(default_factory=dict)

    # -- generic queries -------------------------------------------------------------------

    @property
    def span(self) -> str:
        if self.line_start == self.line_end:
            return f"line {self.line_start}"
        return f"lines {self.line_start}-{self.line_end}"

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
            raise NoSafePatch(
                f"line {lineno} uses tab indentation; the deterministic patcher only re-indents "
                "space-indented code"
            )
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

    def is_builtin(self, name: str) -> bool:
        """``name`` resolves to the builtin of that name inside the target function."""
        return self.is_module_global(name) and not module_bindings(self.tree, name)

    def enclosing_loops(self, node: ast.AST) -> list[LoopNode]:
        """Loops (innermost first) whose per-iteration code contains ``node``, within the target
        function. Refuses when a nested scope or comprehension lies in between."""
        loops: list[LoopNode] = []
        child = node
        parent = self.parents.get(child)
        while parent is not None and parent is not self.function:
            if isinstance(parent, (*_SCOPES, ast.comprehension)):
                owner = self.parents.get(parent, parent)
                scope = owner if isinstance(parent, ast.comprehension) else parent
                raise NoSafePatch(
                    f"the code at line {line_of(node)} is inside {describe_scope(scope)} at line "
                    f"{line_of(scope)}; only code that runs directly in `{self.qualname}`'s own "
                    "`for`/`while` loops is rewritten"
                )
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
        raise NoSafePatch(f"could not locate the block that contains line {stmt.lineno}")

    def parameter(self, name: str) -> ast.arg | None:
        args = self.function.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg is not None:
            params.append(args.vararg)
        if args.kwarg is not None:
            params.append(args.kwarg)
        return next((arg for arg in params if arg.arg == name), None)

    def require_sequence(self, name: str, why: str) -> None:
        """``name`` must be a parameter/local whose bindings provably make it a list or tuple.
        ``why`` completes "this matters because ..." for the refusal message."""
        self.require_collection(name, SEQUENCE, why)

    def require_collection(self, name: str, kind: CollectionKind, why: str) -> None:
        if not self.is_local(name):
            raise NoSafePatch(
                f"`{name}` is not a parameter or local variable of `{self.qualname}` (it is a "
                f"global, closure or builtin name); another function could change it between "
                f"loop iterations, and {why}"
            )
        param = self.parameter(name)
        if param is not None and (
            param.annotation is None
            or not annotation_is(param.annotation, kind.annotations)
            or param is self.function.args.vararg
            or param is self.function.args.kwarg
        ):
            found = (
                "has no annotation"
                if param.annotation is None
                else f"is annotated `{self.source.segment(param.annotation)}`"
            )
            raise NoSafePatch(
                f"cannot prove parameter `{name}` is {kind.label}: it {found} (expected e.g. "
                f"`{name}: list[...]`); {why}"
            )
        problem = binding_problem(self, name, kind)
        if problem is not None:
            raise NoSafePatch(f"cannot prove `{name}` is {kind.label}: {problem}; {why}")
        if param is None and not binding_statements(self, name):
            raise NoSafePatch(f"cannot prove `{name}` is {kind.label}: it is never assigned")

    # -- type evidence from callees ------------------------------------------------------------

    def call_return_annotation(self, call: ast.Call, awaited: bool) -> ast.expr | None:
        """The return annotation of the function ``call`` invokes, when it can be resolved to a
        single undecorated module-level ``def`` in this file or (through ``from x import f``) in
        another file under the source root. ``awaited`` requires an ``async def``."""
        if not isinstance(call.func, ast.Name) or not self.is_module_global(call.func.id):
            return None
        found = self._resolve_function(self.tree, self.path, call.func.id, depth=0)
        if found is None or isinstance(found, ast.AsyncFunctionDef) is not awaited:
            return None
        return found.returns

    def _resolve_function(
        self, tree: ast.Module, path: str, name: str, depth: int
    ) -> FunctionNode | None:
        bindings = module_bindings(tree, name)
        if len(bindings) != 1:
            return None
        binding = bindings[0]
        if isinstance(binding, FunctionNode):
            return binding if not binding.decorator_list else None
        if not isinstance(binding, ast.ImportFrom) or depth >= _MAX_REEXPORT_DEPTH:
            return None
        alias = next(a for a in binding.names if (a.asname or a.name) == name)
        target = self._module_file(path, binding.module, binding.level)
        if target is None:
            return None
        relative, module_tree = target
        return self._resolve_function(module_tree, relative, alias.name, depth + 1)

    def _module_file(
        self, importer: str, module: str | None, level: int
    ) -> tuple[str, ast.Module] | None:
        if self.source_root is None or not module:
            return None
        root = self.source_root.resolve()
        parts = module.split(".")
        importer_dir = (root / importer).parent
        if level > 0:
            base = importer_dir
            for _ in range(level - 1):
                base = base.parent
            bases = [base]
        else:
            bases = [d for d in [importer_dir, *importer_dir.parents] if d.is_relative_to(root)]
        found: dict[Path, str] = {}
        for base in bases:
            for candidate in (
                base.joinpath(*parts[:-1], f"{parts[-1]}.py"),
                base.joinpath(*parts, "__init__.py"),
            ):
                relative = _contained_file(root, candidate)
                if relative is not None:
                    found[candidate.resolve()] = relative
        if len(found) != 1:
            return None  # not under the source root, or ambiguous
        ((resolved, relative),) = found.items()
        if resolved not in self._modules:
            try:
                self._modules[resolved] = ast.parse(resolved.read_text("utf-8"), relative)
            except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
                self._modules[resolved] = None
        module_tree = self._modules[resolved]
        return (relative, module_tree) if module_tree is not None else None


def _contained_file(root: Path, candidate: Path) -> str | None:
    if not candidate.is_relative_to(root):
        return None
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return None
    if not candidate.is_file() or not candidate.resolve().is_relative_to(root):
        return None
    if candidate.stat().st_size > MAX_FILE_BYTES:
        return None
    return candidate.relative_to(root).as_posix()


# ------------------------------------------------------------------------------------------
# Small AST utilities
# ------------------------------------------------------------------------------------------


def line_of(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 0))


def describe_scope(node: ast.AST) -> str:
    if isinstance(node, ast.Lambda):
        return "a lambda"
    if isinstance(node, ast.ClassDef):
        return f"class `{node.name}`"
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return f"nested function `{node.name}`"
    if isinstance(node, ast.GeneratorExp):
        return "a generator expression"
    if isinstance(node, _COMPREHENSIONS):
        return "a comprehension"
    return "a nested scope"


def loop_inside(loop: LoopNode) -> list[ast.AST]:
    """Nodes evaluated once per iteration (a ``for`` header's iterable is evaluated once)."""
    nodes: list[ast.AST] = [*loop.body, *loop.orelse]
    if isinstance(loop, ast.While):
        nodes.insert(0, loop.test)
    return nodes


def loop_kind(loop: LoopNode) -> str:
    if isinstance(loop, ast.AsyncFor):
        return "`async for` loop"
    return "`while` loop" if isinstance(loop, ast.While) else "`for` loop"


def occurrences(nodes: Iterable[ast.AST], name: str) -> list[ast.Name]:
    return [n for node in nodes for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == name]


def own_nodes(nodes: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Walk without descending into nested functions, lambdas or classes."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def suspension_point(loop: LoopNode) -> ast.AST | None:
    """Where the loop can pause and let other code run: ``await``, ``async for``/``async with``
    and ``yield``. Code that runs meanwhile could change a collection indexed before the loop."""
    if isinstance(loop, ast.AsyncFor):
        return loop
    for node in own_nodes([loop.iter if isinstance(loop, ast.For) else loop.test, *loop.body]):
        if isinstance(node, ast.Await | ast.AsyncFor | ast.AsyncWith | ast.Yield | ast.YieldFrom):
            return node
    for node in own_nodes(loop.orelse):
        if isinstance(node, ast.Await | ast.AsyncFor | ast.AsyncWith | ast.Yield | ast.YieldFrom):
            return node
    return None


def describe_suspension(node: ast.AST) -> str:
    if isinstance(node, ast.AsyncFor):
        return f"`async for` at line {node.lineno}"
    if isinstance(node, ast.AsyncWith):
        return f"`async with` at line {node.lineno}"
    if isinstance(node, ast.Yield | ast.YieldFrom):
        return f"`yield` at line {line_of(node)}"
    return f"`await` at line {line_of(node)}"


def require_no_suspension(loop: LoopNode, subject: str) -> None:
    point = suspension_point(loop)
    if point is not None:
        raise NoSafePatch(
            f"the {loop_kind(loop)} at line {loop.lineno} can pause ({describe_suspension(point)})"
            f"; other code may run in between and modify {subject}, which a copy built once "
            "before the loop would not see"
        )


def annotation_node(node: ast.expr) -> ast.expr | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
    return node


def annotation_is(annotation: ast.expr, accepted: frozenset[str]) -> bool:
    node = annotation_node(annotation)
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id in accepted
    if isinstance(node, ast.Attribute):
        return node.attr in accepted and isinstance(node.value, ast.Name)
    return False


def module_bindings(tree: ast.Module, name: str) -> list[ast.AST]:
    """Module-level statements (outside functions and classes) that bind ``name``. A star
    import or a ``global name`` anywhere makes the binding unknowable and yields a sentinel."""
    found: list[ast.AST] = []
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name == name:
                found.append(node)
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            bound = {alias.asname or alias.name.split(".")[0] for alias in node.names}
            if "*" in bound or name in bound:
                found.append(node)
            continue
        if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Global) and name in node.names:
            found.append(node)
    return found


def binding_statements(context: TransformContext, name: str) -> list[ast.AST]:
    """Nodes in the target function that bind ``name`` (assignment targets, loop targets,
    imports, nested defs, except/with aliases, walrus, del)."""
    found: list[ast.AST] = []
    for node in own_nodes([*context.function.body]):
        if _binding_name(context, node) == name:
            found.append(node)
    return sorted(found, key=line_of)


def _binding_name(context: TransformContext, node: ast.AST) -> str | None:
    if isinstance(node, ast.alias):
        return node.asname or node.name.split(".")[0]
    if isinstance(node, FunctionNode | ast.ClassDef):
        return node.name
    if isinstance(node, ast.ExceptHandler):
        return node.name
    if (
        isinstance(node, ast.Name)
        and not isinstance(node.ctx, ast.Load)
        and not _comprehension_target(context, node)
    ):
        return node.id
    return None


def _binds(target: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target))


def _comprehension_target(context: TransformContext, node: ast.Name) -> bool:
    parent = context.parents.get(node)
    while parent is not None and not isinstance(parent, ast.stmt):
        if isinstance(parent, ast.comprehension) and _binds(parent.target, node.id):
            return True
        parent = context.parents.get(parent)
    return False


def binding_problem(context: TransformContext, name: str, kind: CollectionKind) -> str | None:
    """Why the function's bindings of ``name`` do not prove ``kind`` (``None`` if they do)."""
    for node in binding_statements(context, name):
        line = line_of(node)
        if not isinstance(node, ast.Name):
            return f"line {line} binds it by {_binding_construct(node)}"
        parent = context.parents.get(node)
        if isinstance(parent, ast.Assign) and any(t is node for t in parent.targets):
            value: ast.expr | None = parent.value
        elif isinstance(parent, ast.AnnAssign) and parent.target is node:
            value = parent.value
            if value is None:
                if annotation_is(parent.annotation, kind.annotations):
                    continue  # a bare declaration; the assignments are checked on their own
                annotation = context.source.segment(parent.annotation)
                return f"line {line} declares it as `{annotation}`"
        elif isinstance(parent, ast.AugAssign) and parent.target is node:
            continue  # `x += [...]` keeps a list a list (and a tuple a tuple)
        elif isinstance(node.ctx, ast.Del):
            return f"line {line} deletes it"
        else:
            return f"line {line} binds it as {_binding_construct(parent)}"
        problem = value_problem(context, value, kind)
        if problem is not None:
            return f"line {line} assigns it {problem}"
    return None


def _binding_construct(node: ast.AST | None) -> str:
    if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        return "a loop target"
    if isinstance(node, ast.alias):
        return "an import"
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return "a def/class statement"
    if isinstance(node, ast.ExceptHandler):
        return "an `except ... as` clause"
    if isinstance(node, ast.withitem):
        return "a `with ... as` clause"
    if isinstance(node, ast.NamedExpr):
        return "an assignment expression (`:=`)"
    if isinstance(node, ast.Tuple | ast.List | ast.Starred):
        return "tuple unpacking"
    return "a statement the patcher does not analyse"


def value_problem(
    context: TransformContext, value: ast.expr | None, kind: CollectionKind
) -> str | None:
    """``None`` when ``value`` provably evaluates to ``kind``; otherwise what it is instead."""
    if isinstance(value, kind.literals):
        return None
    if isinstance(value, ast.IfExp):
        problem = value_problem(context, value.body, kind) or value_problem(
            context, value.orelse, kind
        )
        return None if problem is None else f"a conditional expression with a branch {problem}"
    awaited = isinstance(value, ast.Await)
    call = value.value if isinstance(value, ast.Await) else value
    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
        callee = call.func.id
        if not awaited and callee in kind.factories and context.is_builtin(callee):
            return None
        returns = context.call_return_annotation(call, awaited)
        if returns is not None and annotation_is(returns, kind.annotations):
            return None
        if returns is not None:
            annotation = ast.unparse(returns)
            return f"from `{callee}()`, which is annotated to return `{annotation}`"
        return (
            f"from `{callee}()`, whose return type could not be resolved to a list/tuple-like "
            "annotation (only undecorated functions defined in this file or imported with "
            "`from module import name` from the repository are followed)"
        )
    if isinstance(call, ast.Call):
        return "from a method or other call whose return type is not known"
    if isinstance(value, ast.Name):
        return f"from another variable (`{value.id}`)"
    return f"from {expression_kind(value)}"


def describe_use(context: TransformContext, node: ast.Name) -> str:
    """How ``node`` (a variable occurrence) is used, e.g. "line 7 calls `seen.append(...)`"."""
    name, line = node.id, node.lineno
    parent = context.parents.get(node)
    if isinstance(node.ctx, ast.Del):
        return f"line {line} deletes `{name}`"
    if isinstance(parent, ast.AugAssign) and parent.target is node:
        return f"line {line} updates `{name}` in place (augmented assignment)"
    if isinstance(node.ctx, ast.Store):
        if isinstance(parent, ast.For | ast.AsyncFor | ast.comprehension):
            return f"line {line} uses `{name}` as a loop variable"
        return f"line {line} assigns a new value to `{name}`"
    if isinstance(parent, ast.Attribute) and parent.value is node:
        grand = context.parents.get(parent)
        if isinstance(grand, ast.Call) and grand.func is parent:
            return f"line {line} calls `{name}.{parent.attr}(...)`"
        if not isinstance(parent.ctx, ast.Load):
            return f"line {line} assigns to `{name}.{parent.attr}`"
        return f"line {line} reads `{name}.{parent.attr}`"
    if isinstance(parent, ast.Subscript) and parent.value is node:
        if not isinstance(parent.ctx, ast.Load):
            return f"line {line} assigns to or deletes `{name}[...]`"
        return f"line {line} indexes or slices `{name}`"
    if isinstance(parent, ast.Call) and node is not parent.func:
        return f"line {line} passes `{name}` to `{_callee(parent)}`"
    if isinstance(parent, ast.keyword):
        call = context.parents.get(parent)
        callee = _callee(call) if isinstance(call, ast.Call) else "a call"
        return f"line {line} passes `{name}` to `{callee}`"
    if isinstance(parent, ast.Yield | ast.YieldFrom):
        return f"line {line} yields `{name}`"
    if isinstance(parent, ast.Return):
        return f"line {line} returns `{name}`"
    if isinstance(parent, ast.Compare):
        return f"line {line} compares `{name}`"
    if isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr):
        return f"line {line} assigns `{name}` to another variable (an alias)"
    if isinstance(parent, ast.FormattedValue | ast.JoinedStr):
        return f"line {line} formats `{name}` into an f-string"
    return f"line {line} reads `{name}`"


def condition_kind(test: ast.expr) -> str:
    if isinstance(test, ast.BoolOp):
        operator = "and" if isinstance(test.op, ast.And) else "or"
        return f"an `{operator}` of {len(test.values)} conditions"
    if isinstance(test, ast.Compare) and len(test.ops) > 1:
        return "a chained comparison"
    if isinstance(test, ast.Compare):
        return f"a `{type(test.ops[0]).__name__}` comparison"
    return f"a `{type(test).__name__}` expression"


def _callee(call: ast.Call | None) -> str:
    if call is None:
        return "a call"
    func = call.func
    if isinstance(func, ast.Name):
        return f"{func.id}()"
    if isinstance(func, ast.Attribute):
        base = func.value.id if isinstance(func.value, ast.Name) else "..."
        return f"{base}.{func.attr}()"
    return "a call"


def expression_kind(node: ast.AST | None) -> str:
    if node is None:
        return "nothing"
    if isinstance(node, ast.Constant):
        kind = type(node.value).__name__
        return f"{'an' if kind[0] in 'aeiou' else 'a'} {kind} constant"
    if isinstance(node, ast.Await):
        return f"an awaited {expression_kind(node.value)}"
    if isinstance(node, ast.Call):
        return f"a call to `{ast.unparse(node.func)}()`"
    if isinstance(node, ast.Name):
        return f"another variable (`{node.id}`)"
    if isinstance(node, ast.JoinedStr):
        return "an f-string"
    if isinstance(node, ast.Attribute):
        return "an attribute"
    if isinstance(node, ast.Subscript):
        return "a subscript"
    if isinstance(node, ast.GeneratorExp):
        return "a generator expression (a one-shot iterator)"
    if isinstance(node, ast.BinOp):
        return "an arithmetic/concatenation expression"
    return f"a `{type(node).__name__}` expression"


def insert_before(
    context: TransformContext, edits: Edits, stmt: ast.stmt, lines: list[str]
) -> None:
    indent = context.indent(stmt.lineno)
    block = "".join(f"{indent}{line}\n" for line in lines)
    edits.insert(context.source.line_start(stmt.lineno), block)


def no_multiline_strings(nodes: Iterable[ast.AST]) -> None:
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Constant | ast.JoinedStr) and child.lineno != child.end_lineno:
                raise NoSafePatch(
                    f"the code to re-indent contains a multi-line string at line {child.lineno}; "
                    "re-indenting it would change the string's content"
                )


def body_step(context: TransformContext, loop: LoopNode) -> str:
    """The indentation step used by ``loop``'s body (four spaces if the body shares the
    header's line)."""
    first = loop.body[0]
    header, body = context.indent(loop.lineno), context.indent(first.lineno)
    if first.lineno > loop.lineno and body.startswith(header) and len(body) > len(header):
        return " " * (len(body) - len(header))
    return "    "


# ------------------------------------------------------------------------------------------
# Context construction
# ------------------------------------------------------------------------------------------


def build_context(
    path: str,
    text: str,
    tree: ast.Module,
    function: FunctionNode,
    qualname: str,
    line_start: int,
    line_end: int,
    source_root: Path | None = None,
) -> TransformContext:
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    scope = _function_scope(symtable.symtable(text, path, "exec"), function, qualname)
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
        source_root,
    )
    _reject_shared_bindings(context)
    return context


def _reject_shared_bindings(context: TransformContext) -> None:
    for node in ast.walk(context.function):
        if isinstance(node, ast.Global | ast.Nonlocal):
            statement = "global" if isinstance(node, ast.Global) else "nonlocal"
            raise NoSafePatch(
                f"`{context.qualname}` declares `{statement} {', '.join(node.names)}` at line "
                f"{node.lineno}; functions that rebind shared variables are not rewritten by the "
                "deterministic patcher"
            )


def _function_scope(
    table: symtable.SymbolTable, function: ast.AST, qualname: str
) -> symtable.SymbolTable:
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
    raise NoSafePatch(f"could not resolve the symbol table of `{qualname}` (line {lineno})")


def first_applicable(
    name: str, transforms: Sequence[Callable[[TransformContext], TransformResult]]
) -> Callable[[TransformContext], TransformResult]:
    """Try ``transforms`` in order. If none applies, report the refusal of every transform that
    recognised its code shape, or (when none did) what each one looked for."""

    def run(context: TransformContext) -> TransformResult:
        missing: list[str] = []
        refused: list[str] = []
        for transform in transforms:
            try:
                return transform(context)
            except NotApplicable as exc:
                missing.append(str(exc))
            except NoSafePatch as exc:
                refused.append(str(exc))
        raise NoSafePatch("; ".join(refused or missing))

    run.__name__ = name
    return run
