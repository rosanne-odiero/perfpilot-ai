"""Safe mechanical rewrites for a few rules, expressed as line edits on the real source.

Each rewrite re-parses the indexed file text, locates the exact AST node behind the finding, and
returns edits only when every precondition for a behavior-preserving change holds. Anything
ambiguous returns ``None`` (no diff is better than a wrong diff).

Supported:

* ``PP-REGEX-001`` — hoist a constant ``re.compile(...)`` from a function to a module constant.
* ``PP-LOOP-001`` — membership test against a literal list of constants: use a set literal (inline
  or a local variable that is only ever used for membership tests).
* ``PP-STR-001`` — ``s = ""`` + ``s += piece`` in a loop: collect pieces in a list and join once.
* ``PP-CACHE-001`` — undecorated, top-level, synchronous recursive function: add ``lru_cache``.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

from backend.ai.diffs import Edit
from backend.models.audit_schemas import Finding

_HASHABLE_CONSTANTS = (str, bytes, int, float, complex, bool, type(None))
_UNHASHABLE_HINTS = ("list", "dict", "set", "List", "Dict", "Set", "Mapping", "Sequence")


class SourceFile:
    """One parsed file with helpers to slice by AST (UTF-8 byte) offsets."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.text = text
        self.lines = text.split("\n")
        self.tree = ast.parse(text)
        self.names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)} | {
            a.arg for a in ast.walk(self.tree) if isinstance(a, ast.arg)
        }

    def line(self, number: int) -> str:
        return self.lines[number - 1]

    def char_col(self, number: int, byte_col: int) -> int:
        return len(self.line(number).encode("utf-8")[:byte_col].decode("utf-8", errors="replace"))

    def segment(self, node: ast.expr) -> str | None:
        return ast.get_source_segment(self.text, node)

    def fresh_name(self, base: str) -> str:
        name = base
        counter = 2
        while name in self.names or name in self.text:
            name = f"{base}_{counter}"
            counter += 1
        self.names.add(name)
        return name

    def import_anchor(self) -> int:
        """Line before which new module-level statements go: after the last top-level import, or
        after the module docstring / ``__future__`` imports when there are no imports."""
        last = 0
        body = self.tree.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            last = body[0].end_lineno or body[0].lineno
        for node in body:
            if isinstance(node, ast.Import | ast.ImportFrom):
                last = node.end_lineno or node.lineno
            elif not isinstance(node, ast.Expr) or node is not body[0]:
                break
        if last == 0:
            while last < len(self.lines) and self.lines[last].startswith("#"):
                last += 1  # keep shebang / encoding / license comments first
        return last + 1

    def imports_module(self, module: str) -> str | None:
        """Local name bound to ``import <module>`` at top level, if any."""
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == module:
                        return alias.asname or alias.name
        return None

    def imported_from(self, module: str, name: str) -> str | None:
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == module and node.level == 0:
                for alias in node.names:
                    if alias.name == name:
                        return alias.asname or alias.name
        return None


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _function(
    tree: ast.Module, qualname: str | None
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if not qualname:
        return None
    body: list[ast.stmt] = tree.body
    found: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for part in qualname.split("."):
        match = next(
            (
                n
                for n in body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                and n.name == part
            ),
            None,
        )
        if match is None:
            return None
        found = None if isinstance(match, ast.ClassDef) else match
        body = match.body
    return found


def _own_nodes(func: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))
    return out


# --------------------------------------------------------------------------------------
# PP-REGEX-001
# --------------------------------------------------------------------------------------


def _is_constant_flags(node: ast.expr, re_name: str) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return True
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id == re_name and node.attr.isupper()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_constant_flags(node.left, re_name) and _is_constant_flags(node.right, re_name)
    return False


def hoist_regex(source: SourceFile, finding: Finding) -> list[Edit] | None:
    re_name = source.imports_module("re")
    func = _function(source.tree, finding.location.symbol)
    if re_name is None or func is None:
        return None
    line = finding.location.line_start
    calls = [
        n
        for n in _own_nodes(func)
        if isinstance(n, ast.Call)
        and n.lineno == line
        and n.end_lineno == line
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "compile"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == re_name
    ]
    if len(calls) != 1:
        return None
    call = calls[0]
    if call.keywords or not 1 <= len(call.args) <= 2:
        return None
    pattern = call.args[0]
    if not (isinstance(pattern, ast.Constant) and isinstance(pattern.value, str | bytes)):
        return None
    if len(call.args) == 2 and not _is_constant_flags(call.args[1], re_name):
        return None
    expression = source.segment(call)
    if expression is None:
        return None
    name = source.fresh_name(f"_{func.name.upper()}_PATTERN")
    text = source.line(line)
    start = source.char_col(line, call.col_offset)
    end = source.char_col(line, call.end_col_offset or 0)
    anchor = source.import_anchor()
    if anchor > func.lineno:
        return None
    return [
        Edit(anchor, anchor - 1, ("", f"{name} = {expression}")),
        Edit(line, line, (text[:start] + name + text[end:],)),
    ]


# --------------------------------------------------------------------------------------
# PP-LOOP-001
# --------------------------------------------------------------------------------------


def _constant_list(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.List)
        and bool(node.elts)
        and all(
            isinstance(e, ast.Constant) and isinstance(e.value, _HASHABLE_CONSTANTS)
            for e in node.elts
        )
    )


def _bracket_edits(source: SourceFile, node: ast.List) -> list[Edit] | None:
    end_line = node.end_lineno or node.lineno
    start = source.char_col(node.lineno, node.col_offset)
    end = source.char_col(end_line, node.end_col_offset or 0) - 1
    first, last = source.line(node.lineno), source.line(end_line)
    if first[start : start + 1] != "[" or last[end : end + 1] != "]":
        return None
    if node.lineno == end_line:
        return [
            Edit(
                node.lineno,
                node.lineno,
                (first[:start] + "{" + first[start + 1 : end] + "}" + first[end + 1 :],),
            )
        ]
    return [
        Edit(node.lineno, node.lineno, (first[:start] + "{" + first[start + 1 :],)),
        Edit(end_line, end_line, (last[:end] + "}" + last[end + 1 :],)),
    ]


def _membership_comparators(func: ast.AST) -> list[tuple[ast.Compare, ast.expr]]:
    pairs: list[tuple[ast.Compare, ast.expr]] = []
    for node in _own_nodes(func):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.In | ast.NotIn):
                    pairs.append((node, comparator))
    return pairs


def literal_set_membership(source: SourceFile, finding: Finding) -> list[Edit] | None:
    func = _function(source.tree, finding.location.symbol)
    if func is None:
        return None
    span = range(finding.location.line_start, finding.location.line_end + 1)
    targets = [c for cmp, c in _membership_comparators(func) if cmp.lineno in span]
    inline = [c for c in targets if isinstance(c, ast.List) and _constant_list(c)]
    if len(inline) == 1 and isinstance(inline[0], ast.List):
        return _bracket_edits(source, inline[0])
    names = {c.id for c in targets if isinstance(c, ast.Name)}
    if len(names) != 1:
        return None
    (name,) = names
    nodes = _own_nodes(func)
    assignments = [
        n
        for n in nodes
        if isinstance(n, ast.Assign | ast.AnnAssign | ast.AugAssign)
        and any(
            isinstance(t, ast.Name) and t.id == name
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
        )
    ]
    if len(assignments) != 1 or not isinstance(assignments[0], ast.Assign):
        return None
    assignment = assignments[0]
    if len(assignment.targets) != 1 or not _constant_list(assignment.value):
        return None
    if any(isinstance(n, ast.Global | ast.Nonlocal) and name in n.names for n in nodes):
        return None
    membership = {id(c) for _, c in _membership_comparators(func)}
    loads = [
        n
        for n in nodes
        if isinstance(n, ast.Name) and n.id == name and n is not assignment.targets[0]
    ]
    if not loads or any(id(n) not in membership for n in loads):
        return None  # used for something other than membership tests
    nested = [
        n for n in nodes if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    ]
    if any(isinstance(m, ast.Name) and m.id == name for n in nested for m in ast.walk(n)):
        return None
    assert isinstance(assignment.value, ast.List)
    return _bracket_edits(source, assignment.value)


# --------------------------------------------------------------------------------------
# PP-STR-001
# --------------------------------------------------------------------------------------


def _blocks(node: ast.AST) -> list[list[ast.stmt]]:
    blocks: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        value = getattr(node, field, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            blocks.append(value)
    for handler in getattr(node, "handlers", []) or []:
        blocks.append(handler.body)
    for case in getattr(node, "cases", []) or []:
        blocks.append(case.body)
    return blocks


def _containing_block(root: ast.AST, target: ast.AST) -> tuple[list[ast.stmt], int] | None:
    """The statement list and index of the top-level statement of ``root`` containing ``target``
    — searching every nested block, but never inside nested functions or classes."""
    for block in _blocks(root):
        for position, stmt in enumerate(block):
            if stmt is target or any(n is target for n in ast.walk(stmt)):
                if isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
                    return block, position
                if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    return None
                return _containing_block(stmt, target)
    return None


def string_join(source: SourceFile, finding: Finding) -> list[Edit] | None:
    func = _function(source.tree, finding.location.symbol)
    if func is None:
        return None
    line = finding.location.line_start
    aug = [
        n
        for n in _own_nodes(func)
        if isinstance(n, ast.AugAssign)
        and n.lineno == line
        and isinstance(n.op, ast.Add)
        and isinstance(n.target, ast.Name)
    ]
    if len(aug) != 1:
        return None
    assert isinstance(aug[0].target, ast.Name)
    name = aug[0].target.id
    located = _containing_block(func, aug[0])
    if located is None:
        return None
    block, position = located
    loop = block[position]
    if getattr(loop, "orelse", None):
        return None
    init_index = next(
        (
            i
            for i in range(position - 1, -1, -1)
            if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(block[i]))
        ),
        None,
    )
    if init_index is None:
        return None
    init = block[init_index]
    if not (
        isinstance(init, ast.Assign)
        and len(init.targets) == 1
        and isinstance(init.targets[0], ast.Name)
        and init.targets[0].id == name
        and isinstance(init.value, ast.Constant)
        and isinstance(init.value.value, str)
        and init.lineno == init.end_lineno
    ):
        return None
    loop_nodes = list(ast.walk(loop))
    if any(
        isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef)
        for n in loop_nodes
    ):
        return None
    augmented = [
        n
        for n in loop_nodes
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name) and n.target.id == name
    ]
    targets = {id(n.target) for n in augmented}
    if any(not isinstance(n.op, ast.Add) for n in augmented):
        return None
    if any(isinstance(n, ast.Name) and n.id == name and id(n) not in targets for n in loop_nodes):
        return None  # read or rebound inside the loop
    parts = source.fresh_name(f"{name}_parts")
    edits: list[Edit] = []
    init_line = source.line(init.lineno)
    end = source.char_col(init.lineno, init.end_col_offset or 0)
    initial = "" if init.value.value == "" else (source.segment(init.value) or "")
    edits.append(
        Edit(
            init.lineno,
            init.lineno,
            (f"{_indent(init_line)}{parts} = [{initial}]" + init_line[end:],),
        )
    )
    for node in sorted(augmented, key=lambda n: n.lineno):
        first = source.line(node.lineno)
        end_line = node.value.end_lineno or node.value.lineno
        start = source.char_col(node.lineno, node.col_offset)
        value_start = source.char_col(node.value.lineno, node.value.col_offset)
        value_end = source.char_col(end_line, node.value.end_col_offset or 0)
        if node.value.lineno != node.lineno:
            return None
        if end_line == node.lineno:
            new = (
                first[:start]
                + f"{parts}.append("
                + first[value_start:value_end]
                + ")"
                + first[value_end:]
            )
            edits.append(Edit(node.lineno, node.lineno, (new,)))
        else:
            last = source.line(end_line)
            edits.append(
                Edit(
                    node.lineno,
                    node.lineno,
                    (first[:start] + f"{parts}.append(" + first[value_start:],),
                )
            )
            edits.append(Edit(end_line, end_line, (last[:value_end] + ")" + last[value_end:],)))
    loop_end = loop.end_lineno or loop.lineno
    loop_indent = _indent(source.line(loop.lineno))
    edits.append(Edit(loop_end + 1, loop_end, (f'{loop_indent}{name} = "".join({parts})',)))
    return edits


# --------------------------------------------------------------------------------------
# PP-CACHE-001
# --------------------------------------------------------------------------------------


def memoize_recursion(source: SourceFile, finding: Finding) -> list[Edit] | None:
    qualname = finding.location.symbol
    if not qualname or "." in qualname:
        return None  # methods and nested functions: caching would hold self / closures
    func = _function(source.tree, qualname)
    if not isinstance(func, ast.FunctionDef) or func.decorator_list:
        return None
    arguments = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
    if func.args.vararg or func.args.kwarg or not arguments:
        return None
    for arg in arguments:
        annotation = ast.unparse(arg.annotation) if arg.annotation else ""
        if any(hint in annotation for hint in _UNHASHABLE_HINTS):
            return None
    edits: list[Edit] = []
    decorator: str
    if lru := source.imported_from("functools", "lru_cache"):
        decorator = f"@{lru}(maxsize=None)"
    elif module := source.imports_module("functools"):
        decorator = f"@{module}.lru_cache(maxsize=None)"
    else:
        anchor = source.import_anchor()
        if anchor > func.lineno:
            return None
        edits.append(Edit(anchor, anchor - 1, ("import functools",)))
        decorator = "@functools.lru_cache(maxsize=None)"
    indent = _indent(source.line(func.lineno))
    edits.append(Edit(func.lineno, func.lineno - 1, (f"{indent}{decorator}",)))
    return edits


Rewrite = Callable[[SourceFile, Finding], list[Edit] | None]

REWRITES: dict[str, Rewrite] = {
    "PP-REGEX-001": hoist_regex,
    "PP-LOOP-001": literal_set_membership,
    "PP-STR-001": string_join,
    "PP-CACHE-001": memoize_recursion,
}
