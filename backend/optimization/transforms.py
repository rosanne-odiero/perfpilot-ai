"""Deterministic, AST-guided source transformations for the bundled rule families.

Each transform receives a :class:`TransformContext` (the parsed target file, the target function
and the flagged line span) and either returns text edits or raises :class:`NoSafePatch` with the
reason it refuses. A transform only fires when it can show that the rewrite keeps behavior for
well-formed inputs; remaining semantic risks are returned as notes and repeated in the
recommendation. Formatting and comments outside the edited spans are preserved.

The ``PP-LOOP-002`` lookup transforms (``next(...)`` / ``any(...)`` over a collection inside a
loop) live in :mod:`backend.optimization.lookup_transforms`.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence

from backend.optimization.lookup_transforms import linear_lookup_to_index
from backend.optimization.source_edits import Edits
from backend.optimization.transform_context import (
    LoopNode,
    NoSafePatch,
    NotApplicable,
    TransformContext,
    TransformResult,
    build_context,
    condition_kind,
    describe_use,
    expression_kind,
    first_applicable,
    insert_before,
    line_of,
    loop_inside,
    loop_kind,
    no_multiline_strings,
    occurrences,
    require_no_suspension,
    suspension_point,
)

__all__ = [
    "TRANSFORMS",
    "NoSafePatch",
    "NotApplicable",
    "Transform",
    "TransformContext",
    "TransformResult",
    "build_context",
]

_NOT_IN_LOOP = "is not inside a `for`, `async for` or `while` loop of"


# ------------------------------------------------------------------------------------------
# Shared rewrite helpers
# ------------------------------------------------------------------------------------------


def _hoist_loop(
    context: TransformContext,
    loops: Sequence[LoopNode],
    name: str,
    ok: Callable[[ast.Name], bool],
    *,
    expected: str,
    artifact: str,
) -> LoopNode:
    """Outermost enclosing loop in which every use of ``name`` satisfies ``ok`` and that cannot
    pause (``await``/``yield``) between iterations."""
    hoist: LoopNode | None = None
    for loop in loops:
        bad = next((n for n in occurrences(loop_inside(loop), name) if not ok(n)), None)
        if bad is not None:
            if hoist is None:
                raise NoSafePatch(
                    f"`{name}` is used inside the {loop_kind(loop)} at line {loop.lineno} in "
                    f"other ways than {expected}: {describe_use(context, bad)}; {artifact} "
                    "built once before the loop would not follow such changes"
                )
            break
        if suspension_point(loop) is not None:
            if hoist is None:
                require_no_suspension(loop, f"`{name}`")
            break
        hoist = loop
    assert hoist is not None
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
            raise NoSafePatch(
                f"`{root}` refers to `{module}` at module level but is rebound inside "
                f"`{context.qualname}`, so `{reference}` would not mean `{module}.{name}` there"
            )
        return reference
    if name in context.taken_names:
        raise NoSafePatch(
            f"the name `{name}` is already used in this module for something other than "
            f"`{module}.{name}`; adding `from {module} import {name}` would shadow it"
        )
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
        raise NotApplicable(
            f"expected a single `x in <variable>` or `x not in <variable>` test at "
            f"{context.span}, but found none (chained comparisons and tests against attributes "
            "or calls are not rewritten)"
        )
    first = compares[0]
    assert isinstance(first, ast.Compare)
    operand = first.comparators[0]
    assert isinstance(operand, ast.Name)
    name = operand.id
    loops = context.enclosing_loops(first)
    if not loops:
        raise NoSafePatch(
            f"the membership test against `{name}` at line {first.lineno} {_NOT_IN_LOOP} "
            f"`{context.qualname}`, so there is no repeated scan to remove"
        )
    context.require_sequence(
        name,
        "on a str `in` searches substrings and on a dict it tests keys, so only a list or "
        "tuple can be replaced by a set",
    )
    hoist = _hoist_loop(
        context,
        loops,
        name,
        lambda n: _is_membership_operand(context, n),
        expected=f"membership tests (`x in {name}`)",
        artifact="a set",
    )
    set_name = context.unique_name(f"{name}_set")
    edits = Edits()
    insert_before(context, edits, hoist, [f"{set_name} = set({name})"])
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
        raise NotApplicable(
            f"expected a `<variable>.count(x)` call at {context.span}, but found none (only "
            ".count is rewritten; .index and other methods are not)"
        )
    first = calls[0]
    assert isinstance(first, ast.Call)
    assert isinstance(first.func, ast.Attribute)
    assert isinstance(first.func.value, ast.Name)
    name = first.func.value.id
    loops = context.enclosing_loops(first)
    if not loops:
        raise NoSafePatch(
            f"the `{name}.count()` call at line {first.lineno} {_NOT_IN_LOOP} "
            f"`{context.qualname}`, so there is no repeated count to remove"
        )
    context.require_sequence(
        name,
        "`.count()` counts substrings on a str and may mean anything on other types, so only "
        "a list or tuple can be replaced by a Counter",
    )
    hoist = _hoist_loop(
        context,
        loops,
        name,
        lambda n: _count_call(context, n) is not None,
        expected=f"single-argument `{name}.count(x)` calls",
        artifact="a Counter",
    )
    edits = Edits()
    counter = _ensure_import(context, edits, "collections", "Counter")
    counts_name = context.unique_name(f"{name}_counts")
    insert_before(context, edits, hoist, [f"{counts_name} = {counter}({name})"])
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
    target, value = _simple_assignment(stmt)
    if (
        isinstance(target, ast.Name)
        and target.id == name
        and isinstance(value, ast.Constant)
        and isinstance(value.value, str)
    ):
        return value
    return None


def _simple_assignment(stmt: ast.stmt) -> tuple[ast.expr | None, ast.expr | None]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        return stmt.targets[0], stmt.value
    if isinstance(stmt, ast.AnnAssign) and stmt.simple:
        return stmt.target, stmt.value
    return None, None


def string_concat_to_join(context: TransformContext) -> TransformResult:
    statements = context.nodes_in_span(lambda n: _concat_target(n) is not None)
    if not statements:
        raise NotApplicable(
            f"expected `s += <piece>` or `s = s + <piece>` on a variable at {context.span}, but "
            "found none (prepending, `s = piece + s`, and attribute targets are not rewritten)"
        )
    first = statements[0]
    assert isinstance(first, ast.stmt)
    name = _concat_target(first)
    assert name is not None
    loops = context.enclosing_loops(first)
    if not loops:
        raise NoSafePatch(
            f"the concatenation onto `{name}` at line {first.lineno} {_NOT_IN_LOOP} "
            f"`{context.qualname}`"
        )
    loop, init = _find_string_loop(context, loops, name)
    for ancestor in [loop, *context.ancestors(loop)]:
        if isinstance(ancestor, ast.Try | ast.TryStar | ast.With | ast.AsyncWith):
            construct = "try" if isinstance(ancestor, ast.Try | ast.TryStar) else "with"
            raise NoSafePatch(
                f"the {loop_kind(loop)} at line {loop.lineno} runs inside try/with (`{construct}` "
                f"at line {ancestor.lineno}); if an exception is handled mid-loop the original "
                f"keeps the partially built `{name}`, which the rewrite would not"
            )
    orelse = {id(n) for stmt in loop.orelse for n in ast.walk(stmt)}
    appends: list[tuple[ast.stmt, ast.expr]] = []
    for occurrence in occurrences(loop_inside(loop), name):
        statement = context.parents.get(occurrence)
        if isinstance(statement, ast.BinOp):
            statement = context.parents.get(statement)
        expression = _concat_statement(statement, name) if statement is not None else None
        if statement is None or expression is None or not _is_concat_operand(statement, occurrence):
            region = (
                f"the `else` block of the {loop_kind(loop)} at line {loop.lineno}"
                if id(occurrence) in orelse
                else f"the {loop_kind(loop)} at line {loop.lineno}"
            )
            if isinstance(occurrence.ctx, ast.Load):
                raise NoSafePatch(
                    f"`{name}` is read inside {region} ({describe_use(context, occurrence)}), "
                    "not only extended; with a list of parts each such read would need its own "
                    f'`"".join(...)`, so collecting parts would not be faster'
                )
            raise NoSafePatch(
                f"`{name}` is rebound inside {region} ({describe_use(context, occurrence)}) "
                f"other than by `{name} += ...`; the rewrite requires it to only grow"
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
                            raise NoSafePatch(_not_literal_init(previous, name, loop))
                        return loop, init
            statement = context.parents[statement]
    raise NoSafePatch(
        f"`{name}` is not initialised to a string literal before the {loop_kind(loops[0])} at "
        f"line {loops[0].lineno} (it is a parameter or is set outside the enclosing blocks); "
        f'the rewrite needs `{name} = "..."` before the loop to prove `{name}` is a str'
    )


def _not_literal_init(previous: ast.stmt, name: str, loop: LoopNode) -> str:
    target, value = _simple_assignment(previous)
    expected = (
        f'the rewrite needs a string literal initialisation (such as `{name} = ""`) as the last '
        f"use of `{name}` before the loop to prove it is a str (`+=` on a list or number means "
        "something else)"
    )
    if isinstance(target, ast.Name) and target.id == name and value is not None:
        found = f"is set from {expression_kind(value)} at line {previous.lineno}"
    else:
        found = f"is last used at line {previous.lineno} without a string literal assignment"
    return f"before the {loop_kind(loop)} at line {loop.lineno}, `{name}` {found}; {expected}"


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


def _re_aliases(context: TransformContext) -> tuple[set[str], set[str]]:
    """Top-level aliases of ``re`` usable in the function, and those shadowed inside it."""
    aliases = set()
    for stmt in context.tree.body:
        if isinstance(stmt, ast.Import):
            aliases |= {alias.asname or alias.name for alias in stmt.names if alias.name == "re"}
    usable = {alias for alias in aliases if context.is_module_global(alias)}
    return usable, aliases - usable


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
    aliases, shadowed = _re_aliases(context)
    if not aliases:
        if shadowed:
            raise NoSafePatch(
                f"`{sorted(shadowed)[0]}` is imported as `re` at module level but rebound inside "
                f"`{context.qualname}`, so a module-level compiled pattern would not be equivalent"
            )
        raise NotApplicable(
            "the module has no top-level `import re` (a `from re import ...` import or an "
            "import inside a function is not rewritten)"
        )
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
        functions = ", ".join(sorted(_RE_FLAGS_POSITION))
        raise NotApplicable(
            f"expected a `re.<function>(pattern, ...)` call at {context.span} with one of: "
            f"{functions}; found none"
        )
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
        raise NoSafePatch(
            f"the `re` call at line {line_of(calls[0])} {_NOT_IN_LOOP} `{context.qualname}`; "
            "re's own pattern cache already covers a single call"
        )
    anchor, _ = _import_anchor(context)
    if anchor is None or not isinstance(anchor, ast.Import | ast.ImportFrom):
        raise NoSafePatch(
            f"no top-level import statement precedes `{context.qualname}`, so there is no safe "
            "place to define the compiled pattern before it is used"
        )
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
    where = f"`re.{call.func.attr}()` at line {call.lineno}"
    if not call.args or any(isinstance(a, ast.Starred) for a in call.args):
        raise NoSafePatch(
            f"{where} must receive the pattern as its first positional argument (found "
            "*args or no positional argument)"
        )
    if any(k.arg is None or k.arg == "pattern" for k in call.keywords):
        raise NoSafePatch(
            f"{where} uses **kwargs or `pattern=`; only positional patterns are rewritten"
        )
    pattern = call.args[0]
    if not (isinstance(pattern, ast.Constant) and isinstance(pattern.value, str | bytes)):
        raise NoSafePatch(
            f"the pattern passed to {where} is not a string literal (it is "
            f"{expression_kind(pattern)}), so it cannot be compiled once at import time"
        )
    flags_position = _RE_FLAGS_POSITION[call.func.attr]
    rest = call.args[1:]
    flags: ast.expr | None = None
    if len(rest) > flags_position:
        flags = rest[flags_position]
        if len(rest) > flags_position + 1:
            raise NoSafePatch(
                f"{where} has more positional arguments than `re.{call.func.attr}` accepts"
            )
        rest = rest[:flags_position]
    keywords = []
    for keyword_arg in call.keywords:
        if keyword_arg.arg == "flags":
            flags = keyword_arg.value
        else:
            keywords.append(keyword_arg)
    if flags is not None and not _static_flags(flags, aliases):
        raise NoSafePatch(
            f"the flags of {where} are not built only from `re.<FLAG>` constants and integers, "
            "so they cannot be fixed at import time"
        )
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
    inner_loops = context.nodes_in_span(
        lambda n: isinstance(n, ast.For) and context.line_start <= n.lineno <= context.line_end
    )
    inner = next((loop for loop in reversed(inner_loops) if context.enclosing_loops(loop)), None)
    if inner is None:
        raise NotApplicable(
            f"expected a `for` loop nested inside another loop starting at {context.span}, but "
            "found none"
        )
    assert isinstance(inner, ast.For)
    outer = context.enclosing_loops(inner)[0]
    if not isinstance(outer, ast.For | ast.AsyncFor) or not any(
        stmt is inner for stmt in outer.body
    ):
        raise NoSafePatch(
            f"the inner `for` loop at line {inner.lineno} must be a direct statement of an outer "
            f"`for` loop; it is nested in the {loop_kind(outer)} at line {outer.lineno} through "
            "other statements"
            if isinstance(outer, ast.For | ast.AsyncFor)
            else f"the inner `for` loop at line {inner.lineno} is inside a `while` loop (line "
            f"{outer.lineno}); only `for` loops are rewritten"
        )
    if inner.orelse or len(inner.body) != 1 or not isinstance(inner.body[0], ast.If):
        found = (
            "a `for ... else`"
            if inner.orelse
            else f"{len(inner.body)} statement(s) starting with `{type(inner.body[0]).__name__}`"
        )
        raise NoSafePatch(
            f"the inner loop body at line {inner.lineno} must be exactly one `if` with no "
            f"`else`; found {found}"
        )
    condition = inner.body[0]
    if condition.orelse:
        raise NoSafePatch(
            f"the join condition at line {condition.lineno} has an `else` branch, which runs for "
            "non-matching items and cannot be skipped by an index"
        )
    if not isinstance(inner.target, ast.Name) or not isinstance(inner.iter, ast.Name):
        raise NoSafePatch(
            f"the inner loop at line {inner.lineno} must be `for <name> in <variable>:` "
            "(unpacking targets and iterables such as calls, attributes or enumerate() are not "
            "rewritten)"
        )
    item, collection = inner.target.id, inner.iter.id
    test = condition.test
    if not (
        isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
    ):
        raise NoSafePatch(
            f"the join condition must be a single `==` comparison; line {condition.lineno} has "
            f"{condition_kind(test)}"
        )
    left, right = test.left, test.comparators[0]
    if item in _names(left) and item not in _names(right):
        inner_key, outer_key = left, right
    elif item in _names(right) and item not in _names(left):
        inner_key, outer_key = right, left
    else:
        raise NoSafePatch(
            f"exactly one side of the `==` at line {condition.lineno} must depend on the inner "
            f"loop variable `{item}`"
        )
    if _names(inner_key) != {item} or not _pure_key(inner_key) or not _pure_key(outer_key):
        raise NoSafePatch(
            f"join keys must be plain names, attributes, or constant subscripts (line "
            f"{condition.lineno}); calls and computed indexes may have side effects or change "
            "between evaluations"
        )
    context.require_sequence(
        collection,
        "the inner collection is scanned again for every outer item, and only a list or tuple "
        "is guaranteed to yield the same items each time (an iterator would be exhausted)",
    )
    body_nodes: list[ast.AST] = list(condition.body)
    _check_join_body(context, body_nodes, item, _names(outer_key))
    uses = occurrences(loop_inside(outer), collection)
    other = next((use for use in uses if use is not inner.iter), None)
    if other is not None:
        raise NoSafePatch(
            f"`{collection}` is used inside the outer loop other than as the join input "
            f"({describe_use(context, other)}); an index built once could go stale"
        )
    require_no_suspension(outer, f"`{collection}`")
    for use in occurrences([context.function], item):
        if isinstance(use.ctx, ast.Load) and not _within(context, use, inner):
            raise NoSafePatch(
                f"`{item}` is read outside the inner loop at line {use.lineno}; after the "
                "rewrite it would hold the last matching item instead of the last scanned one"
            )
    first_body = condition.body[0]
    header_end = test.end_lineno or condition.lineno
    if first_body.lineno <= header_end:
        raise NoSafePatch(
            f"the body of the `if` at line {condition.lineno} must start on its own line"
        )
    no_multiline_strings(body_nodes)
    if_indent = context.indent(condition.lineno)
    body_indent = context.indent(first_body.lineno)
    if not body_indent.startswith(if_indent) or len(body_indent) <= len(if_indent):
        raise NoSafePatch(
            f"the join body at line {first_body.lineno} is not indented deeper than its `if`"
        )
    dedent = len(body_indent) - len(if_indent)
    step = " " * dedent

    index = context.unique_name(f"{collection}_by_{_key_label(inner_key)}")
    inner_key_text = context.source.segment(inner_key)
    edits = Edits()
    insert_before(
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
            raise NoSafePatch(
                f"line {lineno} of the join body is indented less than the body's first line"
            )
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
            line = getattr(child, "lineno", "?")
            if isinstance(child, ast.Name) and stores and child.id in outer_names:
                raise NoSafePatch(
                    f"the join body rebinds `{child.id}` at line {line}, and the outer join key "
                    "uses it"
                )
            if (
                isinstance(child, ast.Subscript | ast.Attribute)
                and stores
                and item in _names(child.value)
            ):
                raise NoSafePatch(
                    f"the join body modifies `{item}` at line {line}, which may change its key "
                    "after it was indexed"
                )
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and item in _names(child.func.value)
            ):
                raise NoSafePatch(
                    f"the join body calls a method on `{item}` at line {line}; it could change "
                    "the item's key after it was indexed"
                )


Transform = Callable[[TransformContext], TransformResult]

TRANSFORMS: dict[str, Transform] = {
    "PP-LOOP-001": membership_to_set,
    "PP-COLL-001": list_count_to_counter,
    "PP-STR-001": string_concat_to_join,
    "PP-REGEX-001": regex_compile_hoist,
    "PP-LOOP-002": first_applicable(
        "nested_loop_or_lookup_to_index", [nested_loop_to_index, linear_lookup_to_index]
    ),
}
