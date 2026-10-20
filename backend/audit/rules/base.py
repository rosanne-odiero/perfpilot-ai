"""Rule framework: rule metadata, finding drafts, and the scope-aware single-pass traversal.

Every rule is a :class:`BaseRule` subclass with ``visit_<NodeType>(node, scope)`` hooks. The
:class:`ScopeTracker` walks each module once and calls the hooks of all rules with a
:class:`ScopeTracker` describing where the node sits: enclosing function (sync/async), class,
loop stack (``for``/``while``/comprehensions, reset at every function boundary), names assigned or
mutated inside each loop, and inferred kinds of names.

Evaluation-order details matter for precision: a ``for`` loop's iterable and a comprehension's
first iterable are evaluated once, outside the loop, so they are visited before the loop frame is
pushed; a ``while`` test runs every iteration, so it is visited inside.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from backend.audit.rules.kinds import Kind, KindInference
from backend.indexing.ast_index import iter_own_nodes
from backend.indexing.entrypoints import is_main_guard
from backend.indexing.names import ImportAliases, root_name
from backend.indexing.source import ParsedModule
from backend.models.enums import Confidence, FindingCategory, Severity

COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

PURE_FUNCTIONS = frozenset(
    {"len", "sorted", "sum", "min", "max", "any", "all", "abs", "round", "str", "repr", "int"}
    | {"float", "bool", "tuple", "list", "set", "frozenset", "dict", "isinstance", "issubclass"}
    | {"hasattr", "getattr", "type", "id", "hash", "print", "format", "enumerate", "zip"}
    | {"range", "reversed", "map", "filter", "ord", "chr", "divmod", "pow", "callable", "bytes"}
    | {"json.dumps", "json.loads", "copy.copy", "copy.deepcopy", "functools.reduce"}
)
PURE_MODULE_PREFIXES = ("math.", "os.path.", "re.", "operator.", "itertools.", "string.")
NON_MUTATING_METHODS = frozenset(
    {"get", "keys", "values", "items", "count", "index", "copy", "startswith", "endswith"}
    | {"lower", "upper", "strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "join"}
    | {"format", "replace", "find", "rfind", "encode", "decode", "isdigit", "isalpha", "isalnum"}
    | {"isspace", "title", "capitalize", "casefold", "zfill", "partition", "rpartition"}
    | {"issubset", "issuperset", "union", "intersection", "difference", "isoformat"}
    | {"symmetric_difference", "strftime", "timestamp", "total_seconds", "to_dict", "model_dump"}
    | {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
RECEIVER_ONLY_METHODS = frozenset(
    {"append", "extend", "insert", "add", "update", "setdefault", "remove", "discard", "put"}
    | {"appendleft", "extendleft", "write", "writelines", "send", "put_nowait"}
)


@dataclass(frozen=True)
class RuleMeta:
    rule_id: str
    title: str
    category: FindingCategory


@dataclass
class FindingDraft:
    meta: RuleMeta
    line_start: int
    line_end: int
    code: str
    description: str
    severity: Severity
    confidence: Confidence
    suggestion: str
    tradeoffs: str
    complexity_note: str | None
    symbol: str | None
    loop_depth: int


@dataclass
class LoopFrame:
    node: ast.AST
    kind: Literal["for", "while", "comprehension"]
    assigned: set[str]
    mutated: set[str]
    has_await: bool

    @property
    def line(self) -> int:
        return getattr(self.node, "lineno", 0)


@dataclass
class Frame:
    kind: Literal["module", "class", "function", "lambda"]
    node: ast.AST
    qualname: str | None
    is_async: bool = False
    kinds: dict[str, Kind] = field(default_factory=dict)
    declared_outer: set[str] = field(default_factory=set)
    loops: list[LoopFrame] = field(default_factory=list)
    class_frame: Frame | None = None
    self_name: str | None = None
    attr_kinds: dict[str, Kind] = field(default_factory=dict)


def _target_names(node: ast.AST) -> Iterator[str]:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            yield sub.id


def _is_pure_callee(qualified: str | None) -> bool:
    return bool(qualified) and (
        qualified in PURE_FUNCTIONS or str(qualified).startswith(PURE_MODULE_PREFIXES)
    )


def loop_effects(nodes: list[ast.AST], aliases: ImportAliases) -> tuple[set[str], set[str], bool]:
    """Names (re)bound, names possibly mutated, and whether the loop awaits."""
    assigned: set[str] = set()
    mutated: set[str] = set()
    has_await = False
    for node in iter_own_nodes(nodes):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            assigned.add(node.id)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            assigned.update(node.names)
        elif isinstance(node, ast.Await | ast.Yield | ast.YieldFrom):
            has_await = True
        elif isinstance(node, ast.Attribute | ast.Subscript) and isinstance(
            node.ctx, ast.Store | ast.Del
        ):
            if (root := root_name(node.value)) is not None:
                mutated.add(root)
        elif isinstance(node, ast.Call):
            _call_effects(node, aliases, mutated)
    return assigned, mutated, has_await


def _call_effects(node: ast.Call, aliases: ImportAliases, mutated: set[str]) -> None:
    func = node.func
    args_safe = _is_pure_callee(aliases.qualify(func))
    if isinstance(func, ast.Attribute):
        if func.attr not in NON_MUTATING_METHODS and (root := root_name(func.value)):
            mutated.add(root)
        args_safe = args_safe or func.attr in NON_MUTATING_METHODS | RECEIVER_ONLY_METHODS
    if args_safe:
        return
    for arg in [*node.args, *(k.value for k in node.keywords)]:
        if (root := root_name(arg.value if isinstance(arg, ast.Starred) else arg)) is not None:
            mutated.add(root)


class BaseRule:
    """Base class for rules. Subclasses set ``meta`` and implement ``visit_<Node>`` hooks."""

    meta: ClassVar[RuleMeta]
    suggestion: ClassVar[str] = ""
    tradeoffs: ClassVar[str] = ""

    def begin_module(self, module: ParsedModule) -> None:
        """Reset per-module state."""

    def end_module(self, scope: ScopeTracker) -> None:
        """Emit findings that need the whole module."""

    def emit(
        self,
        scope: ScopeTracker,
        node: ast.AST,
        *,
        description: str,
        severity: Severity,
        confidence: Confidence,
        complexity_note: str | None = None,
        suggestion: str | None = None,
        tradeoffs: str | None = None,
        line_end: int | None = None,
        symbol: str | None = None,
    ) -> None:
        line_start = getattr(node, "lineno", 1)
        end = line_end or getattr(node, "end_lineno", None) or line_start
        scope.drafts.append(
            FindingDraft(
                meta=self.meta,
                line_start=line_start,
                line_end=max(end, line_start),
                code=ast.unparse(node) if isinstance(node, ast.AST) else "",
                description=description,
                severity=severity,
                confidence=confidence,
                suggestion=suggestion or self.suggestion,
                tradeoffs=tradeoffs or self.tradeoffs,
                complexity_note=complexity_note,
                symbol=symbol or scope.symbol,
                loop_depth=0 if symbol else scope.loop_depth,
            )
        )


Hook = Callable[[ast.AST, "ScopeTracker"], None]


class ScopeTracker:
    """Single traversal of a module that dispatches nodes to rule hooks with scope context."""

    def __init__(self, module: ParsedModule, rules: list[BaseRule]) -> None:
        assert module.tree is not None
        self.module = module
        self.tree = module.tree
        self.aliases = module.aliases
        self.infer = KindInference(module.aliases)
        self.rules = rules
        self.drafts: list[FindingDraft] = []
        self.frames: list[Frame] = []
        self.ancestors: list[ast.AST] = []
        self.in_main_guard = False
        self.in_type_checking = False
        self.store_counts: dict[str, int] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.store_counts[node.id] = self.store_counts.get(node.id, 0) + 1
        self.module_values: dict[str, ast.expr] = {}
        for stmt in self.tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            else:
                continue
            if isinstance(target, ast.Name) and self.store_counts.get(target.id) == 1:
                self.module_values[target.id] = value
        self._hooks: dict[str, list[Hook]] = {}
        for rule in rules:
            for name in dir(rule):
                if name.startswith("visit_"):
                    self._hooks.setdefault(name[6:], []).append(getattr(rule, name))

    # ----------------------------------------------------------------------------- queries
    @property
    def frame(self) -> Frame:
        return self.frames[-1]

    @property
    def function(self) -> Frame | None:
        for frame in reversed(self.frames):
            if frame.kind in {"function", "lambda"}:
                return frame
            if frame.kind == "class":
                return None
        return None

    @property
    def symbol(self) -> str | None:
        for frame in reversed(self.frames):
            if frame.kind in {"function", "class"}:
                return frame.qualname
        return None

    @property
    def loops(self) -> list[LoopFrame]:
        return self.frame.loops

    @property
    def loop_depth(self) -> int:
        return len(self.frame.loops)

    @property
    def in_loop(self) -> bool:
        return bool(self.frame.loops)

    @property
    def innermost_loop(self) -> LoopFrame | None:
        return self.frame.loops[-1] if self.frame.loops else None

    @property
    def in_async_function(self) -> bool:
        return self.frame.kind == "function" and self.frame.is_async

    @property
    def at_import_time(self) -> bool:
        """Module body or class body executed on import (not under ``__main__``/TYPE_CHECKING)."""
        return (
            all(f.kind in {"module", "class"} for f in self.frames)
            and not self.in_main_guard
            and not self.in_type_checking
        )

    @property
    def parent(self) -> ast.AST | None:
        return self.ancestors[-2] if len(self.ancestors) >= 2 else None

    def is_loop_iterable(self, node: ast.AST) -> bool:
        """True when ``node`` is the iterable of a ``for`` loop or comprehension generator."""
        parent = self.parent
        if isinstance(parent, ast.For | ast.AsyncFor):
            return parent.iter is node
        if isinstance(parent, COMPREHENSION_NODES):
            return any(gen.iter is node for gen in parent.generators)
        return False

    def qualify(self, node: ast.expr) -> str | None:
        return self.aliases.qualify(node)

    def name_kind(self, name: str) -> Kind:
        for frame in reversed(self.frames):
            if frame.kind == "class":
                continue
            if name in frame.declared_outer:
                continue
            if name in frame.kinds:
                return frame.kinds[name]
        return Kind.UNKNOWN

    def attr_kind(self, node: ast.Attribute) -> Kind:
        function = self.function
        if function is None or function.class_frame is None:
            return Kind.UNKNOWN
        if isinstance(node.value, ast.Name) and node.value.id == function.self_name:
            return function.class_frame.attr_kinds.get(node.attr, Kind.UNKNOWN)
        return Kind.UNKNOWN

    def kind(self, node: ast.expr) -> Kind:
        return self.infer.expr_kind(node, self.name_kind, self.attr_kind)

    def module_value(self, name: str) -> ast.expr | None:
        """The single module-level value bound to ``name`` when it is not shadowed locally."""
        if self.is_local(name):
            return None
        return self.module_values.get(name)

    def is_constant(self, node: ast.expr) -> bool:
        """Literal constants, containers of constants, f-strings without fields, and names bound
        exactly once at module level to such a value."""
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            return all(self.is_constant(e) for e in node.elts)
        if isinstance(node, ast.JoinedStr):
            return not any(isinstance(v, ast.FormattedValue) for v in node.values)
        if isinstance(node, ast.Name):
            value = self.module_value(node.id)
            return value is not None and not isinstance(value, ast.Name) and self.is_constant(value)
        return False

    def is_param(self, name: str) -> bool:
        function = self.function
        if function is None or not isinstance(
            function.node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ):
            return False
        args = function.node.args
        all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        return any(a is not None and a.arg == name for a in all_args)

    def is_local(self, name: str) -> bool:
        function = self.function
        return function is not None and name in function.kinds

    def is_invariant(self, expr: ast.expr, loop: LoopFrame) -> bool:
        """True when ``expr`` provably evaluates the same in every iteration of ``loop``."""
        bound_inside_expr: set[str] = set()
        for node in ast.walk(expr):
            if isinstance(node, ast.Await | ast.Yield | ast.YieldFrom | ast.NamedExpr):
                return False
            if isinstance(node, ast.comprehension):
                bound_inside_expr.update(_target_names(node.target))
            if isinstance(node, ast.Lambda):
                bound_inside_expr.update(a.arg for a in [*node.args.posonlyargs, *node.args.args])
            if isinstance(node, ast.Call) and not _is_pure_callee(self.qualify(node.func)):
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr in NON_MUTATING_METHODS):
                    return False
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and node.id not in bound_inside_expr:
                if node.id in loop.assigned or node.id in loop.mutated:
                    return False
                if node.id in {"self", "cls"} and loop.has_await:
                    return False
        return True

    # ----------------------------------------------------------------------------- traversal
    def run(self) -> list[FindingDraft]:
        module_frame = Frame(kind="module", node=self.tree, qualname=None)
        self.frames.append(module_frame)
        module_frame.kinds, module_frame.declared_outer = self.infer.scope_kinds(
            self.tree.body, None, lambda _n: Kind.UNKNOWN, lambda _a: Kind.UNKNOWN
        )
        for rule in self.rules:
            rule.begin_module(self.module)
        self.visit(self.tree)
        for rule in self.rules:
            rule.end_module(self)
        self.frames.pop()
        return self.drafts

    def visit(self, node: ast.AST) -> None:
        self.ancestors.append(node)
        try:
            for hook in self._hooks.get(type(node).__name__, ()):
                hook(node, self)
            handler = getattr(self, f"_visit_{type(node).__name__}", None)
            if handler is not None:
                handler(node)
            else:
                for child in ast.iter_child_nodes(node):
                    self.visit(child)
        finally:
            self.ancestors.pop()

    def _visit_all(self, nodes: list[ast.stmt] | list[ast.expr] | list[ast.AST]) -> None:
        for node in nodes:
            self.visit(node)

    def _push_loop(
        self, node: ast.AST, kind: Literal["for", "while", "comprehension"], body: list[ast.AST]
    ) -> None:
        assigned, mutated, has_await = loop_effects(body, self.aliases)
        self.frame.loops.append(LoopFrame(node, kind, assigned, mutated, has_await))

    def _visit_For(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._push_loop(node, "for", [node.target, *node.body])
        self.visit(node.target)
        self._visit_all(node.body)
        self.frame.loops.pop()
        self._visit_all(node.orelse)

    _visit_AsyncFor = _visit_For

    def _visit_While(self, node: ast.While) -> None:
        self._push_loop(node, "while", [node.test, *node.body])
        self.visit(node.test)
        self._visit_all(node.body)
        self.frame.loops.pop()
        self._visit_all(node.orelse)

    def _visit_comprehension_node(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> None:
        first, *rest = node.generators
        self.visit(first.iter)
        results: list[ast.AST] = (
            [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        )
        inner: list[ast.AST] = [first.target, *first.ifs]
        for gen in rest:
            inner.extend([gen.iter, gen.target, *gen.ifs])
        self._push_loop(node, "comprehension", inner + results)
        self._visit_all(inner + results)
        self.frame.loops.pop()

    _visit_ListComp = _visit_comprehension_node
    _visit_SetComp = _visit_comprehension_node
    _visit_DictComp = _visit_comprehension_node
    _visit_GeneratorExp = _visit_comprehension_node

    def qualname_for(self, name: str) -> str:
        """Qualified name of a definition called ``name`` in the current scope."""
        parent = self.symbol
        return f"{parent}.{name}" if parent else name

    def _visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_all(node.decorator_list)
        self._visit_arguments_outside(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        enclosing = self.frame
        class_frame = enclosing if enclosing.kind == "class" else None
        positional = [*node.args.posonlyargs, *node.args.args]
        self_name = positional[0].arg if class_frame is not None and positional else None
        frame = Frame(
            kind="function",
            node=node,
            qualname=self.qualname_for(node.name),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            class_frame=class_frame,
            self_name=self_name,
        )
        self.frames.append(frame)
        frame.kinds, frame.declared_outer = self.infer.scope_kinds(
            node.body, node.args, self._outer_lookup(), self.attr_kind
        )
        self._visit_all(node.body)
        self.frames.pop()

    _visit_AsyncFunctionDef = _visit_FunctionDef

    def _visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_arguments_outside(node.args)
        frame = Frame(kind="lambda", node=node, qualname=self.symbol)
        frame.kinds = {a.arg: Kind.UNKNOWN for a in [*node.args.posonlyargs, *node.args.args]}
        self.frames.append(frame)
        self.visit(node.body)
        self.frames.pop()

    def _visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_all(node.decorator_list)
        self._visit_all(node.bases)
        self._visit_all([k.value for k in node.keywords])
        frame = Frame(kind="class", node=node, qualname=self.qualname_for(node.name))
        frame.attr_kinds = self._class_attr_kinds(node)
        self.frames.append(frame)
        self._visit_all(node.body)
        self.frames.pop()

    def _visit_arguments_outside(self, args: ast.arguments) -> None:
        defaults: list[ast.expr] = [*args.defaults, *(d for d in args.kw_defaults if d)]
        self._visit_all(defaults)

    def _visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        top_level = len(self.frames) == 1
        guard = top_level and is_main_guard(node)
        type_checking = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
            isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
        )
        previous = (self.in_main_guard, self.in_type_checking)
        self.in_main_guard = self.in_main_guard or guard
        self.in_type_checking = self.in_type_checking or type_checking
        self._visit_all(node.body)
        self.in_main_guard, self.in_type_checking = previous
        self._visit_all(node.orelse)

    def _outer_lookup(self) -> Callable[[str], Kind]:
        return self.name_kind

    def _class_attr_kinds(self, node: ast.ClassDef) -> dict[str, Kind]:
        kinds: dict[str, Kind] = {}

        def bind(name: str, kind: Kind) -> None:
            previous = kinds.get(name)
            kinds[name] = kind if previous is None or previous is kind else Kind.UNKNOWN

        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                kind = self.infer.annotation_kind(stmt.annotation)
                if kind is Kind.UNKNOWN and stmt.value is not None:
                    kind = self.kind(stmt.value)
                bind(stmt.target.id, kind)
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        bind(target.id, self.kind(stmt.value))
        for method in node.body:
            if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            positional = [*method.args.posonlyargs, *method.args.args]
            if not positional:
                continue
            self_name = positional[0].arg
            local_kinds, _ = self.infer.scope_kinds(
                method.body, method.args, self.name_kind, lambda _a: Kind.UNKNOWN
            )

            def lookup(name: str, local_kinds: dict[str, Kind] = local_kinds) -> Kind:
                return local_kinds.get(name, self.name_kind(name))

            for sub in iter_own_nodes(method.body):
                targets: list[ast.expr] = []
                value: ast.expr | None = None
                if isinstance(sub, ast.Assign):
                    targets, value = sub.targets, sub.value
                elif isinstance(sub, ast.AnnAssign):
                    targets, value = [sub.target], sub.value
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == self_name
                    ):
                        kind = Kind.UNKNOWN
                        if isinstance(sub, ast.AnnAssign):
                            kind = self.infer.annotation_kind(sub.annotation)
                        if kind is Kind.UNKNOWN and value is not None:
                            kind = self.infer.expr_kind(value, lookup, lambda _a: Kind.UNKNOWN)
                        bind(target.attr, kind)
        return kinds
