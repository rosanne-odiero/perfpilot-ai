"""PP-LOOP-002 linear lookups inside a loop -> an index built once before the loop.

* ``next((VALUE(x) for x in SEQ if KEY(x) == PROBE), DEFAULT)`` becomes a first-match dict
  ``{KEY(x): x}`` built before the loop and ``(VALUE(idx[PROBE]) if PROBE in idx else DEFAULT)``.
* ``any(KEY(x) == PROBE for x in SEQ)`` becomes a set ``{KEY(x) for x in SEQ}`` and
  ``(PROBE in keys)``.

The rewrite is only proposed when the analysis can show that ``SEQ`` is a re-iterable collection
that neither changes nor is modified (its elements' key fields included) while the loop runs, and
that every expression it moves or re-orders is free of side effects.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

from backend.optimization.source_edits import Edits
from backend.optimization.transform_context import (
    REITERABLE,
    LoopNode,
    NoSafePatch,
    NotApplicable,
    TransformContext,
    TransformResult,
    annotation_is,
    binding_statements,
    body_step,
    condition_kind,
    describe_scope,
    describe_suspension,
    describe_use,
    expression_kind,
    insert_before,
    line_of,
    loop_kind,
    occurrences,
    own_nodes,
    suspension_point,
)

Step = tuple[str, str]  # ("attr", name) or ("item", repr(constant))

_CONVERTERS = frozenset({"str", "int", "float", "len"})
_RAISING_CONVERTERS = frozenset({"int", "float", "len"})
_READ_METHODS = frozenset({"get", "keys", "values", "items", "copy", "count", "index"})
_PURE_FUNCTIONS = _CONVERTERS | {"bool", "repr", "isinstance", "round", "abs", "print"}
_MUTATORS = frozenset(
    {"update", "clear", "pop", "popitem", "setdefault", "__setitem__", "__delitem__"}
)
_FIELD_MUTATORS = frozenset({"pop", "setdefault", "__setitem__", "__delitem__"})
_FRESH_FACTORIES = frozenset({"list", "dict", "set"})
_UNHASHABLE = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
_TRY_LIKE = (ast.Try, ast.TryStar, ast.With, ast.AsyncWith)
_VALUE_NODES = (
    ast.Name, ast.Constant, ast.Attribute, ast.Subscript, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.IfExp, ast.Tuple, ast.List, ast.Dict, ast.Set, ast.JoinedStr,
    ast.FormattedValue, ast.Call, ast.Slice, ast.expr_context, ast.operator, ast.unaryop,
    ast.boolop, ast.cmpop,
)  # fmt: skip
_PROBE_NODES = (
    ast.Name, ast.Constant, ast.Attribute, ast.Subscript, ast.BinOp, ast.UnaryOp, ast.Tuple,
    ast.Call, ast.Slice, ast.expr_context, ast.operator, ast.unaryop,
)  # fmt: skip


@dataclass
class _Lookup:
    call: ast.Call
    kind: Literal["next", "any"]
    generator: ast.GeneratorExp
    var: str
    seq: ast.Name | ast.Attribute
    root: str
    key: ast.expr
    key_path: list[Step]
    probe: ast.expr
    value: ast.expr | None
    default: ast.expr | None

    @property
    def seq_label(self) -> str:
        return self.seq.id if isinstance(self.seq, ast.Name) else self.seq.attr


def linear_lookup_to_index(context: TransformContext) -> TransformResult:
    call = _find_call(context)
    lookup = _parse(context, call)
    _require_own_scope(context, call)
    loops = context.enclosing_loops(call)
    if not loops:
        raise NoSafePatch(
            f"the `{lookup.kind}(...)` lookup at line {call.lineno} is not inside a `for`, "
            f"`async for` or `while` loop of `{context.qualname}`, so it runs only once and an "
            "index would not save anything"
        )
    _check_value_and_default(context, lookup)
    key_may_raise = _may_raise(lookup.key)
    if key_may_raise:
        for ancestor in context.ancestors(call):
            if isinstance(ancestor, _TRY_LIKE):
                construct = "try" if isinstance(ancestor, ast.Try | ast.TryStar) else "with"
                raise NoSafePatch(
                    f"the lookup at line {call.lineno} runs inside `{construct}` (line "
                    f"{ancestor.lineno}) and its key `{_text(context, lookup.key)}` can raise "
                    "(attribute access, indexing or int()/float()/len()); the index evaluates "
                    "the key for every element before the loop, so an exception would surface "
                    "at a different point and skip the partial work the handler expects"
                )
    rebound = _rebinding_problem(context, lookup, loops[0])
    if rebound is not None:
        raise NoSafePatch(rebound)
    evidence = _require_collection(context, lookup)
    hoist = _hoist_loop(context, lookup, loops)
    if lookup.kind == "next":
        result = _rewrite_next(context, lookup, hoist)
    else:
        result = _rewrite_any(context, lookup, hoist)
    result.notes.extend(_common_notes(context, lookup, hoist, key_may_raise, evidence))
    return result


# ------------------------------------------------------------------------------------------
# Recognising the lookup
# ------------------------------------------------------------------------------------------


def _find_call(context: TransformContext) -> ast.Call:
    calls = context.nodes_in_span(
        lambda n: (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"next", "any"}
            and bool(n.args)
            and isinstance(n.args[0], ast.GeneratorExp)
            and context.line_start <= n.args[0].lineno <= context.line_end
        )
    )
    if calls:
        first = calls[0]
        assert isinstance(first, ast.Call)
        return first
    expected = (
        f"expected a `next((... for x in <collection> if <key> == <value>), <default>)` or "
        f"`any(<key> == <value> for x in <collection>)` lookup at {context.span}"
    )
    comprehensions = context.nodes_in_span(
        lambda n: (
            isinstance(n, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp)
            and context.line_start <= n.lineno <= context.line_end
        )
    )
    if comprehensions:
        found = comprehensions[0]
        parent = context.parents.get(found)
        if isinstance(found, ast.GeneratorExp) and isinstance(parent, ast.Call):
            what = f"a generator passed to `{ast.unparse(parent.func)}()`"
        else:
            what = "a comprehension that collects every match"
        raise NotApplicable(
            f"{expected}; found {what} at line {line_of(found)} instead (only first-match "
            "`next(...)` and `any(...)` lookups are rewritten)"
        )
    raise NotApplicable(f"{expected}; found none")


def _require_own_scope(context: TransformContext, call: ast.Call) -> None:
    """The lookup must run in the loop's own scope, not in a lambda or comprehension body."""
    for ancestor in context.ancestors(call):
        if isinstance(ancestor, ast.Lambda | ast.ListComp | ast.SetComp | ast.DictComp):
            raise NoSafePatch(
                f"the lookup at line {call.lineno} is inside {describe_scope(ancestor)} at line "
                f"{line_of(ancestor)}; only lookups that run directly in the loop body are "
                "rewritten"
            )
        if isinstance(ancestor, ast.GeneratorExp):
            raise NoSafePatch(
                f"the lookup at line {call.lineno} is inside {describe_scope(ancestor)} at line "
                f"{line_of(ancestor)}, which may run lazily after the loop has moved on"
            )


def _parse(context: TransformContext, call: ast.Call) -> _Lookup:
    assert isinstance(call.func, ast.Name)
    kind: Literal["next", "any"] = "next" if call.func.id == "next" else "any"
    line = call.lineno
    if not context.is_builtin(kind):
        raise NoSafePatch(
            f"`{kind}` at line {line} is not the builtin `{kind}()` (the name is rebound in "
            "this module or function)"
        )
    expected_args = (1, 2) if kind == "next" else (1,)
    if call.keywords or len(call.args) not in expected_args:
        raise NoSafePatch(f"`{kind}()` at line {line} has unexpected arguments")
    generator = call.args[0]
    assert isinstance(generator, ast.GeneratorExp)
    if len(generator.generators) != 1:
        raise NoSafePatch(
            f"the generator at line {generator.lineno} has {len(generator.generators)} `for` "
            "clauses; only a single `for <name> in <collection>` is rewritten"
        )
    clause = generator.generators[0]
    if clause.is_async:
        raise NoSafePatch(f"the generator at line {generator.lineno} uses `async for`")
    if not isinstance(clause.target, ast.Name):
        raise NoSafePatch(
            f"the generator at line {generator.lineno} unpacks its loop target; only "
            "`for <name> in <collection>` is rewritten"
        )
    var = clause.target.id
    value: ast.expr | None = None
    default: ast.expr | None = None
    if kind == "next":
        if len(clause.ifs) != 1:
            raise NoSafePatch(
                f"the generator at line {generator.lineno} has {len(clause.ifs)} `if` clauses; "
                "exactly one `if <key> == <value>` is required"
            )
        if len(call.args) == 1:
            raise NoSafePatch(
                f"`next()` at line {line} has no default, so it raises StopIteration when "
                "nothing matches; the rewrite would raise KeyError instead. Add an explicit "
                "default (for example `None`) if a missing match is expected"
            )
        test, value, default = clause.ifs[0], generator.elt, call.args[1]
    else:
        if clause.ifs:
            raise NoSafePatch(
                f"`any()` at line {line} filters with `if`; only `any(<key> == <value> for x in "
                "<collection>)` is rewritten"
            )
        test = generator.elt
    if not (
        isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
    ):
        raise NoSafePatch(
            f"the lookup condition at line {line_of(test)} must be a single `==` comparison; "
            f"found {condition_kind(test)}"
        )
    left, right = test.left, test.comparators[0]
    left_uses, right_uses = var in _names(left), var in _names(right)
    if left_uses == right_uses:
        raise NoSafePatch(
            f"exactly one side of the `==` at line {line_of(test)} must depend on the "
            f"generator variable `{var}` (found it on "
            f"{'both sides' if left_uses else 'neither side'})"
        )
    key, probe = (left, right) if left_uses else (right, left)
    key_path = _key_path(context, key, var)
    if key_path is None:
        raise NoSafePatch(_key_problem(context, key, var))
    _check_probe(context, probe)
    seq = clause.iter
    if isinstance(seq, ast.Name):
        root = seq.id
    elif (
        isinstance(seq, ast.Attribute)
        and isinstance(seq.value, ast.Name)
        and seq.value.id == _self_name(context)
    ):
        root = seq.value.id
    else:
        raise NoSafePatch(
            f"the collection searched at line {line_of(seq)} must be a variable or "
            f"`self.<attribute>`; found {expression_kind(seq)}, which may be recomputed or "
            "changed by each evaluation"
        )
    if root == var:
        raise NoSafePatch(f"the generator at line {generator.lineno} iterates its own variable")
    return _Lookup(call, kind, generator, var, seq, root, key, key_path, probe, value, default)


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _text(context: TransformContext, node: ast.AST) -> str:
    text = context.source.segment(node)
    return text if len(text) <= 60 else text[:59] + "…"


def _hashable_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(
        node.value, str | bytes | int | float | bool | type(None)
    )


def _access_path(node: ast.AST, var: str) -> list[Step] | None:
    """Steps from the generator variable for ``x``, ``x.attr``, ``x[const]`` and
    ``x.get(const[, const])`` chains; ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return [] if node.id == var and isinstance(node.ctx, ast.Load) else None
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        base = _access_path(node.value, var)
        return None if base is None else [*base, ("attr", node.attr)]
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        if not _hashable_constant(node.slice):
            return None
        base = _access_path(node.value, var)
        assert isinstance(node.slice, ast.Constant)
        return None if base is None else [*base, ("item", repr(node.slice.value))]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and not node.keywords
        and 1 <= len(node.args) <= 2
        and _hashable_constant(node.args[0])
        and all(isinstance(arg, ast.Constant) for arg in node.args[1:])
    ):
        base = _access_path(node.func.value, var)
        first = node.args[0]
        assert isinstance(first, ast.Constant)
        return None if base is None else [*base, ("item", repr(first.value))]
    return None


def _converter(context: TransformContext, node: ast.AST) -> ast.expr | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _CONVERTERS
        and context.is_builtin(node.func.id)
        and len(node.args) == 1
        and not isinstance(node.args[0], ast.Starred)
        and not node.keywords
    ):
        return node.args[0]
    return None


def _key_path(context: TransformContext, node: ast.expr, var: str) -> list[Step] | None:
    inner: ast.expr = node
    while (unwrapped := _converter(context, inner)) is not None:
        inner = unwrapped
    path = _access_path(inner, var)
    return path or None  # comparing the whole item is a membership test, not a field lookup


def _key_problem(context: TransformContext, key: ast.expr, var: str) -> str:
    where = f"the key `{_text(context, key)}` at line {line_of(key)}"
    if isinstance(key, _UNHASHABLE):
        return f"{where} builds a list/dict/set, which is unhashable and cannot index a dict"
    if isinstance(key, ast.Name) and key.id == var:
        return (
            f"{where} compares the whole item; only lookups by a field of the item (attribute, "
            "constant subscript or `.get(<constant>)`) are rewritten"
        )
    calls = [n for n in ast.walk(key) if isinstance(n, ast.Call) and _converter(context, n) is None]
    if calls:
        callee = ast.unparse(calls[0].func)
        return (
            f"{where} calls `{callee}()`; only attribute access, constant subscripts, "
            "`.get(<constant>[, <constant>])` and str()/int()/float()/len() are allowed, because "
            "the index evaluates the key once per element before the loop"
        )
    others = sorted(_names(key) - {var} - _CONVERTERS)
    if others:
        return (
            f"{where} also depends on `{others[0]}`; the key must depend only on the generator "
            f"variable `{var}` so it can be computed once before the loop"
        )
    return (
        f"{where} must read the item through attributes, constant subscripts or "
        "`.get(<constant>[, <constant>])` (optionally wrapped in str()/int()/float()/len())"
    )


def _pure(
    context: TransformContext, node: ast.expr, allowed: tuple[type[ast.AST], ...]
) -> ast.AST | None:
    """The first construct in ``node`` that may have side effects, if any."""
    for child in ast.walk(node):
        if not isinstance(child, allowed):
            return child
        if isinstance(child, ast.Call):
            is_get = (
                isinstance(child.func, ast.Attribute)
                and child.func.attr == "get"
                and not child.keywords
                and 1 <= len(child.args) <= 2
            )
            if _converter(context, child) is None and not is_get:
                return child
    return None


def _describe_impure(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return f"a call to `{ast.unparse(node.func)}()`"
    return f"a `{type(node).__name__}` expression"


def _check_probe(context: TransformContext, probe: ast.expr) -> None:
    where = f"the value `{_text(context, probe)}` compared at line {line_of(probe)}"
    if isinstance(probe, _UNHASHABLE):
        raise NoSafePatch(
            f"{where} is a list/dict/set, which is unhashable and cannot be looked up"
        )
    impure = _pure(context, probe, _PROBE_NODES)
    if impure is not None:
        raise NoSafePatch(
            f"{where} contains {_describe_impure(impure)}; the generator evaluated it once per "
            "compared element, the rewrite once per lookup, so it must be free of side effects"
        )


def _check_value_and_default(context: TransformContext, lookup: _Lookup) -> None:
    if lookup.value is not None:
        impure = _pure(context, lookup.value, _VALUE_NODES)
        if impure is not None:
            raise NoSafePatch(
                f"the value produced at line {line_of(lookup.value)} contains "
                f"{_describe_impure(impure)}, which could modify the matching item after it was "
                "indexed"
            )
    if lookup.default is not None and _pure(context, lookup.default, _VALUE_NODES) is not None:
        raise NoSafePatch(
            f"the default `{_text(context, lookup.default)}` at line "
            f"{line_of(lookup.default)} is evaluated by next() before searching, even when "
            "an item matches; the rewrite evaluates it only on a miss, so it must be a "
            "constant or another side-effect-free expression"
        )


def _may_raise(key: ast.expr) -> bool:
    """Whether evaluating the key on some element can raise (``.get`` on a mapping cannot)."""
    get_methods = {
        id(n.func)
        for n in ast.walk(key)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
    }
    for node in ast.walk(key):
        if isinstance(node, ast.Subscript):
            return True
        if isinstance(node, ast.Attribute) and id(node) not in get_methods:
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _RAISING_CONVERTERS
        ):
            return True
    return False


def _self_name(context: TransformContext) -> str | None:
    parent = context.parents.get(context.function)
    args = context.function.args.posonlyargs + context.function.args.args
    if not isinstance(parent, ast.ClassDef) or not args:
        return None
    decorators = {ast.unparse(d) for d in context.function.decorator_list}
    if decorators & {"staticmethod", "classmethod"}:
        return None
    return args[0].arg


# ------------------------------------------------------------------------------------------
# Evidence about the searched collection
# ------------------------------------------------------------------------------------------

_WHY = (
    "the index is built from a single pass over it before the loop, so it must yield the same "
    "items every time it is iterated (an iterator or generator would be consumed differently)"
)


def _require_collection(context: TransformContext, lookup: _Lookup) -> list[str]:
    """Prove the collection is re-iterable; return notes about annotation-based evidence."""
    if isinstance(lookup.seq, ast.Name):
        name = lookup.seq.id
        context.require_collection(name, REITERABLE, _WHY)
        return _annotation_evidence(context, name)
    attribute = lookup.seq.attr
    declared = _declared_attribute(context, attribute)
    if declared is None:
        raise NoSafePatch(
            f"cannot prove `{lookup.root}.{attribute}` is {REITERABLE.label}: the class does not "
            f"declare `{attribute}: list[...]` (or another collection type) in its body or as "
            f"`self.{attribute}: ... = ...`; {_WHY}"
        )
    return [
        f"`self.{attribute}` is treated as a collection because the class declares it as "
        f"`{declared}`; if it can hold an iterator, the rewrite is not equivalent."
    ]


def _declared_attribute(context: TransformContext, attribute: str) -> str | None:
    owner = context.parents.get(context.function)
    assert isinstance(owner, ast.ClassDef)
    for node in ast.walk(owner):
        target: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if (
                isinstance(target, ast.Name)
                and target.id == attribute
                and node in owner.body
                and annotation_is(node.annotation, REITERABLE.annotations)
            ) or (
                isinstance(target, ast.Attribute)
                and target.attr == attribute
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and annotation_is(node.annotation, REITERABLE.annotations)
            ):
                return ast.unparse(node.annotation)
    return None


def _annotation_evidence(context: TransformContext, name: str) -> list[str]:
    notes: list[str] = []
    for node in binding_statements(context, name):
        parent = context.parents.get(node)
        value = parent.value if isinstance(parent, ast.Assign | ast.AnnAssign) else None
        awaited = isinstance(value, ast.Await)
        call = value.value if isinstance(value, ast.Await) else value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            returns = context.call_return_annotation(call, awaited)
            if returns is not None and call.func.id not in REITERABLE.factories:
                notes.append(
                    f"`{name}` is treated as a collection because `{call.func.id}()` is "
                    f"annotated to return `{ast.unparse(returns)}`; if it can return an iterator "
                    "or generator, the rewrite is not equivalent."
                )
    return notes


# ------------------------------------------------------------------------------------------
# Choosing the loop to hoist before, and proving nothing changes inside it
# ------------------------------------------------------------------------------------------


def _hoist_loop(context: TransformContext, lookup: _Lookup, loops: list[LoopNode]) -> LoopNode:
    hoist: LoopNode | None = None
    for loop in loops:
        problem = _loop_problem(context, lookup, loop)
        if problem is not None:
            if hoist is None:
                raise NoSafePatch(problem)
            break
        hoist = loop
    assert hoist is not None
    return hoist


def _rebinding_problem(context: TransformContext, lookup: _Lookup, loop: LoopNode) -> str | None:
    for node in binding_statements(context, lookup.root):
        if _inside(context, node, loop):
            return (
                f"`{lookup.root}` is (re)bound inside the {loop_kind(loop)} at line "
                f"{loop.lineno} at line {line_of(node)}, so `{context.source.segment(lookup.seq)}` "
                "can differ between iterations and cannot be indexed once before the loop"
            )
    return None


def _loop_problem(context: TransformContext, lookup: _Lookup, loop: LoopNode) -> str | None:
    seq_text = context.source.segment(lookup.seq)
    where = f"the {loop_kind(loop)} at line {loop.lineno}"
    rebound = _rebinding_problem(context, lookup, loop)
    if rebound is not None:
        return rebound
    if isinstance(lookup.seq, ast.Name) and not _bound_before(context, lookup.root, loop):
        return (
            f"`{lookup.root}` is not assigned on every path before {where} (it is assigned "
            "only in a branch, or deleted); building the index before the loop could fail where "
            "the original never evaluated the lookup"
        )
    header = loop.iter if isinstance(loop, ast.For) else None
    for use in occurrences([loop], lookup.root):
        if _is_lookup_iter(lookup, use) or (use is header and isinstance(lookup.seq, ast.Name)):
            continue  # the lookup itself, or the loop iterating the same collection once
        if isinstance(lookup.seq, ast.Attribute) and _plain_attribute_read(
            context, use, lookup.seq.attr
        ):
            continue
        return (
            f"`{seq_text}` is used inside {where} other than by the lookup at line "
            f"{lookup.call.lineno} ({describe_use(context, use)}); it could change, or hand out "
            "its items, while the loop runs"
        )
    point = suspension_point(loop)
    if point is not None:
        return (
            f"{where} can pause ({describe_suspension(point)}); other code may run in between "
            f"and modify `{seq_text}` or its items, which an index built once before the loop "
            "would not see"
        )
    mutation = _key_mutation(context, lookup, loop)
    if mutation is not None:
        return mutation
    if lookup.kind == "next" and _value_aliases_key(context, lookup):
        escape = _result_escape(context, lookup, loop)
        if escape is not None:
            return escape
    return None


def _inside(context: TransformContext, node: ast.AST, container: ast.AST) -> bool:
    current: ast.AST | None = node
    while current is not None:
        if current is container:
            return True
        current = context.parents.get(current)
    return False


def _is_lookup_iter(lookup: _Lookup, use: ast.Name) -> bool:
    iterable = lookup.generator.generators[0].iter
    return use is iterable or (isinstance(iterable, ast.Attribute) and use is iterable.value)


def _plain_attribute_read(context: TransformContext, use: ast.Name, excluded: str) -> bool:
    """``self.other`` read (not called, assigned, or the searched attribute itself)."""
    parent = context.parents.get(use)
    grand = context.parents.get(parent) if parent is not None else None
    return (
        isinstance(parent, ast.Attribute)
        and isinstance(parent.ctx, ast.Load)
        and parent.attr != excluded
        and not (isinstance(grand, ast.Call) and grand.func is parent)
    )


def _bound_before(context: TransformContext, name: str, loop: LoopNode) -> bool:
    """``name`` is a parameter or is assigned unconditionally before ``loop`` (on every path),
    and never deleted."""
    if any(
        isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Del)
        for n in ast.walk(context.function)
    ):
        return False
    if context.parameter(name) is not None:
        return True
    statement: ast.AST = loop
    while statement is not context.function:
        if isinstance(statement, ast.stmt):
            block, index = context.containing_block(statement)
            for previous in block[:index]:
                if isinstance(previous, ast.Assign | ast.AnnAssign) and previous.value is not None:
                    targets = (
                        previous.targets if isinstance(previous, ast.Assign) else [previous.target]
                    )
                    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                        return True
        statement = context.parents[statement]
    return False


def _fields(lookup: _Lookup) -> set[str]:
    return {value for _kind, value in lookup.key_path}


def _key_mutation(context: TransformContext, lookup: _Lookup, loop: LoopNode) -> str | None:
    """A statement in the loop that could change an item's key fields."""
    fields = _fields(lookup)
    readable = ", ".join(f"`{_field_label(step)}`" for step in lookup.key_path)
    for node in own_nodes([loop]):
        target: ast.expr | None = None
        if isinstance(node, ast.Subscript | ast.Attribute) and not isinstance(node.ctx, ast.Load):
            target = node
        if target is not None:
            changed = _changed_field(target)
            if changed is not None and changed in fields:
                return (
                    f"line {line_of(node)} assigns or deletes a field named "
                    f"{_field_label(_step_of(target))}, which is part of the lookup key "
                    f"({readable}); an item's key could change after it was indexed"
                )
            if changed is None and not _fresh_container(context, target):
                return (
                    f"line {line_of(node)} assigns to `{_text(context, target)}` with a computed "
                    "index on an object the patcher cannot prove is unrelated to the searched "
                    f"items, so it could change their key ({readable})"
                )
        if isinstance(node, ast.Call):
            problem = _mutating_call(context, node, fields, readable)
            if problem is not None:
                return problem
    return None


def _step_of(node: ast.expr) -> Step:
    if isinstance(node, ast.Attribute):
        return ("attr", node.attr)
    assert isinstance(node, ast.Subscript)
    if _hashable_constant(node.slice):
        assert isinstance(node.slice, ast.Constant)
        return ("item", repr(node.slice.value))
    return ("item", "?")


def _field_label(step: Step) -> str:
    kind, value = step
    return f".{value}" if kind == "attr" else f"[{value}]"


def _changed_field(target: ast.expr) -> str | None:
    """The field name a store writes (``x.f`` / ``x["f"]``), or ``None`` when computed."""
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript) and _hashable_constant(target.slice):
        assert isinstance(target.slice, ast.Constant)
        return repr(target.slice.value)
    return None


def _fresh_container(context: TransformContext, target: ast.expr) -> bool:
    """The written object is a local name only ever bound to a new list/dict/set."""
    owner = target.value if isinstance(target, ast.Subscript | ast.Attribute) else target
    if not isinstance(owner, ast.Name) or not context.is_local(owner.id):
        return False
    if context.parameter(owner.id) is not None:
        return False
    bindings = binding_statements(context, owner.id)
    if not bindings:
        return False
    for node in bindings:
        parent = context.parents.get(node)
        value = parent.value if isinstance(parent, ast.Assign | ast.AnnAssign) else None
        fresh = isinstance(
            value, ast.List | ast.Dict | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp
        ) or (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in _FRESH_FACTORIES
            and context.is_builtin(value.func.id)
        )
        if not fresh:
            return False
    return True


def _mutating_call(
    context: TransformContext, call: ast.Call, fields: set[str], readable: str
) -> str | None:
    func = call.func
    if isinstance(func, ast.Name) and func.id in {"setattr", "delattr"}:
        name = call.args[1] if len(call.args) > 1 else None
        if name is None or not (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and name.value not in fields
        ):
            return (
                f"line {call.lineno} calls `{func.id}()`, which could change an item's key "
                f"({readable}) after it was indexed"
            )
        return None
    if not isinstance(func, ast.Attribute) or func.attr not in _MUTATORS:
        return None
    if _fresh_container(context, func):
        return None
    if func.attr in _FIELD_MUTATORS and call.args and _hashable_constant(call.args[0]):
        first = call.args[0]
        assert isinstance(first, ast.Constant)
        if repr(first.value) not in fields:
            return None
    if func.attr == "update" and _update_fields(call) is not None:
        changed = _update_fields(call)
        assert changed is not None
        if not changed & fields:
            return None
    return (
        f"line {call.lineno} calls `.{func.attr}()` on `{_text(context, func.value)}`, which "
        f"the patcher cannot prove is unrelated to the searched items; it could change their "
        f"key ({readable}) after they were indexed"
    )


def _update_fields(call: ast.Call) -> set[str] | None:
    """Field names written by ``x.update({...}, k=v)`` when all are literal."""
    fields = {repr(k.arg) for k in call.keywords if k.arg is not None}
    if any(k.arg is None for k in call.keywords) or len(call.args) > 1:
        return None
    if call.args:
        mapping = call.args[0]
        if not isinstance(mapping, ast.Dict) or not all(
            key is not None and _hashable_constant(key) for key in mapping.keys
        ):
            return None
        fields |= {repr(key.value) for key in mapping.keys if isinstance(key, ast.Constant)}
    return fields


def _loose_path(node: ast.AST, var: str) -> list[Step] | None:
    """Like :func:`_access_path`, but a computed index or ``.get(<expr>)`` is an unknown
    field ``"?"`` instead of ending the chain."""
    if isinstance(node, ast.Name):
        return [] if node.id == var else None
    if isinstance(node, ast.Attribute):
        base = _loose_path(node.value, var)
        return None if base is None else [*base, ("attr", node.attr)]
    if isinstance(node, ast.Subscript):
        base = _loose_path(node.value, var)
        field = repr(node.slice.value) if isinstance(node.slice, ast.Constant) else "?"
        return None if base is None else [*base, ("item", field)]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        base = _loose_path(node.func.value, var)
        first = node.args[0]
        field = repr(first.value) if isinstance(first, ast.Constant) else "?"
        return None if base is None else [*base, ("item", field)]
    return None


def _value_aliases_key(context: TransformContext, lookup: _Lookup) -> bool:
    """Whether the produced value can be the matching item itself or an object on its key's
    access path (so code holding the result could change the key)."""
    assert lookup.value is not None
    key = lookup.key_path
    for node in ast.walk(lookup.value):
        path = _loose_path(node, lookup.var)
        if path is None:
            continue
        parent = context.parents.get(node)
        if (
            parent is not None
            and _inside(context, parent, lookup.value)
            and (
                _loose_path(parent, lookup.var) is not None
                or (isinstance(parent, ast.Attribute) and parent.attr == "get")
            )
        ):
            continue  # an intermediate step of a longer access chain
        if len(path) <= len(key) and all(
            step == expected or (step[0] == expected[0] and step[1] == "?")
            for step, expected in zip(path, key, strict=False)
        ):
            return True
    return False


def _result_escape(context: TransformContext, lookup: _Lookup, loop: LoopNode) -> str | None:
    """With a result that may alias the item, every use of it in the loop must be a read."""
    tainted: set[str] = set()
    changed = True
    holders: list[ast.AST] = [lookup.call]
    while changed:
        changed = False
        for node in own_nodes([loop]):
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
                value = node.value
                if value is None or not _mentions(value, lookup.call, tainted):
                    continue
                targets: list[ast.expr] = (
                    list(node.targets) if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    for name in _names(target):
                        if name not in tainted:
                            tainted.add(name)
                            changed = True
    holders += [n for n in own_nodes([loop]) if isinstance(n, ast.Name) and n.id in tainted]
    for holder in holders:
        problem = _escaping_use(context, holder)
        if problem is not None:
            name = holder.id if isinstance(holder, ast.Name) else "the lookup result"
            return (
                f"`{name}` holds the matching item itself (or an object on its key path), and "
                f"{problem}; that could change the item's key after it was indexed"
            )
    return None


def _mentions(node: ast.AST, call: ast.Call, tainted: set[str]) -> bool:
    return any(
        child is call or (isinstance(child, ast.Name) and child.id in tainted)
        for child in ast.walk(node)
    )


def _escaping_use(context: TransformContext, node: ast.AST) -> str | None:
    parent = context.parents.get(node)
    line = line_of(node)
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        return None
    if isinstance(parent, ast.Attribute) and parent.value is node:
        grand = context.parents.get(parent)
        if not isinstance(parent.ctx, ast.Load):
            return f"line {line} assigns to one of its attributes"
        if isinstance(grand, ast.Call) and grand.func is parent:
            if parent.attr in _READ_METHODS:
                return None
            return f"line {line} calls `.{parent.attr}()` on it"
        return None
    if isinstance(parent, ast.Subscript) and parent.value is node:
        return None if isinstance(parent.ctx, ast.Load) else f"line {line} assigns into it"
    if isinstance(parent, ast.Call) and node is not parent.func:
        if isinstance(parent.func, ast.Name) and parent.func.id in _PURE_FUNCTIONS:
            return None
        return f"line {line} passes it to `{ast.unparse(parent.func)}()`"
    if isinstance(parent, ast.Assign | ast.AnnAssign | ast.NamedExpr):
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        if all(isinstance(t, ast.Name) for t in targets):
            return None  # the alias is followed separately
        return f"line {line} stores it in a container or attribute"
    if isinstance(
        parent,
        ast.Compare
        | ast.BoolOp
        | ast.UnaryOp
        | ast.If
        | ast.While
        | ast.IfExp
        | ast.Assert
        | ast.FormattedValue
        | ast.Return
        | ast.Expr,
    ):
        return None
    return f"line {line} uses it in a `{type(parent).__name__}` expression"


# ------------------------------------------------------------------------------------------
# Rewrites
# ------------------------------------------------------------------------------------------


def _substitute(context: TransformContext, expression: ast.expr, var: str, text: str) -> str:
    start = context.source.start(expression)
    source = context.source.segment(expression)
    pieces: list[str] = []
    cursor = 0
    for name in sorted(occurrences([expression], var), key=context.source.start):
        offset = context.source.start(name) - start
        pieces += [source[cursor:offset], text]
        cursor = context.source.end(name) - start
    pieces.append(source[cursor:])
    return "".join(pieces)


def _key_label(lookup: _Lookup) -> str:
    return lookup.key_path[-1][1].strip("'\"")


def _rewrite_next(context: TransformContext, lookup: _Lookup, hoist: LoopNode) -> TransformResult:
    assert lookup.value is not None
    assert lookup.default is not None
    index = context.unique_name(f"_{lookup.seq_label}_by_{_key_label(lookup)}")
    item = context.unique_name(f"_{lookup.var}")
    seq_text = context.source.segment(lookup.seq)
    key_text = _substitute(context, lookup.key, lookup.var, item)
    edits = Edits()
    insert_before(
        context,
        edits,
        hoist,
        [
            f"{index} = {{}}",
            f"for {item} in {seq_text}:",
            f"{body_step(context, hoist)}{index}.setdefault({key_text}, {item})",
        ],
    )
    probe = lookup.probe
    simple = isinstance(probe, ast.Name | ast.Constant)
    if simple:
        probe_test = probe_ref = context.source.segment(probe)
    else:
        probe_ref = context.unique_name("_probe")
        probe_test = f"({probe_ref} := {context.source.segment(probe)})"
    value_text = _substitute(context, lookup.value, lookup.var, f"{index}[{probe_ref}]")
    default_text = context.source.segment(lookup.default)
    edits.replace(
        context.source.start(lookup.call),
        context.source.end(lookup.call),
        f"({value_text} if {probe_test} in {index} else {default_text})",
    )
    notes = [
        f"First-match semantics are kept: `setdefault` stores the first element of `{seq_text}` "
        f"for each key, exactly the element `next()` returned.",
    ]
    if not simple:
        notes.append(
            f"The probe `{_text(context, probe)}` is now evaluated once per lookup (bound to "
            f"`{probe_ref}`); the generator evaluated it once per compared element and not at "
            "all when the collection was empty."
        )
    if not isinstance(lookup.default, ast.Constant):
        notes.append(
            f"The default `{_text(context, lookup.default)}` is now evaluated only when nothing "
            "matches (next() evaluated it on every call)."
        )
    return TransformResult(
        name="next_lookup_to_index",
        edits=edits,
        summary=(
            f"Index `{seq_text}` by `{_text(context, lookup.key)}` once into `{index}` before "
            f"the loop at line {hoist.lineno} (first match per key) and replace the linear "
            f"`next(...)` scan at line {lookup.call.lineno} with a dict lookup: O(n + m) "
            "instead of O(n * m)."
        ),
        notes=notes,
    )


def _rewrite_any(context: TransformContext, lookup: _Lookup, hoist: LoopNode) -> TransformResult:
    keys = context.unique_name(f"_{lookup.seq_label}_{_key_label(lookup)}_set")
    item = context.unique_name(f"_{lookup.var}")
    seq_text = context.source.segment(lookup.seq)
    key_text = _substitute(context, lookup.key, lookup.var, item)
    edits = Edits()
    insert_before(context, edits, hoist, [f"{keys} = {{{key_text} for {item} in {seq_text}}}"])
    edits.replace(
        context.source.start(lookup.call),
        context.source.end(lookup.call),
        f"({context.source.segment(lookup.probe)} in {keys})",
    )
    notes = []
    if not isinstance(lookup.probe, ast.Name | ast.Constant):
        notes.append(
            f"The probe `{_text(context, lookup.probe)}` is now evaluated once per test; any() "
            "evaluated it once per compared element and not at all for an empty collection."
        )
    return TransformResult(
        name="any_lookup_to_key_set",
        edits=edits,
        summary=(
            f"Collect the keys `{_text(context, lookup.key)}` of `{seq_text}` once into the set "
            f"`{keys}` before the loop at line {hoist.lineno} and replace the linear `any(...)` "
            f"scan at line {lookup.call.lineno} with a set membership test: O(n + m) instead of "
            "O(n * m)."
        ),
        notes=notes,
    )


def _common_notes(
    context: TransformContext,
    lookup: _Lookup,
    hoist: LoopNode,
    key_may_raise: bool,
    evidence: list[str],
) -> list[str]:
    seq_text = context.source.segment(lookup.seq)
    key = _text(context, lookup.key)
    probe = _text(context, lookup.probe)
    notes = [
        f"Keys must be hashable: `{key}` is now a dict/set key and `{probe}` is hashed for the "
        "lookup; an unhashable value (list, dict) raises TypeError where `==` only compared it.",
        "Matching uses hash + == (stored key compared first, identity short-circuits): equal "
        "keys such as 1 and 1.0 still match, but types whose __hash__ disagrees with __eq__, "
        "or a NaN probe that is the very same object as a stored NaN, behave differently.",
        f"`{key}` is evaluated once for every element of `{seq_text}` before the loop at line "
        f"{hoist.lineno}, instead of only until the first match; an element on which it "
        + (
            "raises (for example one missing the field) now fails even if the original stopped "
            "earlier."
            if key_may_raise
            else "raises (for example one that is not a mapping) now fails even if the original "
            "stopped earlier."
        ),
        f"`{seq_text}` is iterated even when the loop runs zero times.",
        f"`{seq_text}` and its elements must not change while the loop runs; the patcher found "
        "no change in the loop itself, but a function called from it that modifies them through "
        "another reference would leave the index stale.",
        "Extra memory: O(n) for one entry per distinct key.",
    ]
    return [*evidence, *notes]
