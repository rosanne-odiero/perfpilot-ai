"""PP-LOOP-002 lookup transforms: `next(...)`/`any(...)` scans inside a loop -> index built once."""

from __future__ import annotations

import ast
import asyncio
import random
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.audit.rule_engine import run_rules
from backend.indexing.source import parse_file
from backend.optimization.diffs import apply_file_patch, parse_unified_diff
from backend.optimization.patch_generator import PatchProposal, PatchTarget, generate_patch
from backend.optimization.patch_validator import validate_patch


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip())


def _propose(root: Path, relative: str = "mod.py", *, from_rule: bool = True) -> PatchProposal:
    """Patch the single PP-LOOP-002 finding of ``relative`` (or the whole first function)."""
    if from_rule:
        findings = [
            item.finding
            for item in run_rules([parse_file(root, relative)]).findings
            if item.finding.rule_id == "PP-LOOP-002"
        ]
        assert len(findings) == 1, findings
        location = findings[0].location
        start, end, symbol = location.line_start, location.line_end, location.symbol
    else:
        tree = ast.parse((root / relative).read_text())
        function = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        start, end, symbol = function.lineno, function.end_lineno or function.lineno, None
    return generate_patch(
        PatchTarget(
            source_root=root,
            target_file=relative,
            target_function=symbol,
            line_start=start,
            line_end=end,
            rule_id="PP-LOOP-002",
        )
    )


def _accepted(
    root: Path, proposal: PatchProposal, relative: str = "mod.py", *, execute: bool = True
) -> dict[str, Any]:
    """Assert the proposal is a valid, compiling patch and return the patched module namespace."""
    assert proposal.status == "proposed", proposal.reason
    original = (root / relative).read_text()
    (file_patch,) = parse_unified_diff(proposal.diff)
    assert apply_file_patch(original, file_patch) == proposal.patched_source
    compile(proposal.patched_source, relative, "exec", dont_inherit=True)
    validated = validate_patch(proposal.diff, workspace_root=root, allowed_files=[relative])
    assert validated.report.valid, validated.report.violations
    assert (root / relative).read_text() == original
    namespace: dict[str, Any] = {}
    if execute:
        exec(compile(proposal.patched_source, relative, "exec"), namespace)
    return namespace


def _original(root: Path, relative: str = "mod.py") -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(compile((root / relative).read_text(), relative, "exec"), namespace)
    return namespace


def _rows(rng: random.Random, size: int) -> list[dict[str, int]]:
    rows = []
    for position in range(size):
        row = {"Count": rng.randint(0, 99), "pos": position}
        if rng.random() < 0.85:  # some rows lack the key entirely
            key = rng.randint(0, 6)  # small range: plenty of duplicates
            row["binIndex"] = float(key) if rng.random() < 0.2 else key  # 2.0 == 2 matches too
        rows.append(row)
    return rows


HISTOGRAM = """
def histogram(hist_results: list[dict], bins: int) -> list[int]:
    labels = []
    data = []
    for i in range(bins):
        labels.append(f"bin {i}")
        bin_count = next((r.get("Count", 0) for r in hist_results if r.get("binIndex") == i), -1)
        data.append(bin_count)
    return data
"""


def test_next_lookup_becomes_first_match_index(tmp_path: Path) -> None:
    _write(tmp_path, "mod.py", HISTOGRAM)
    proposal = _propose(tmp_path)
    assert proposal.transform == "next_lookup_to_index"
    patched = _accepted(tmp_path, proposal)
    source = proposal.patched_source
    assert "_hist_results_by_binIndex = {}" in source
    assert "for _r in hist_results:" in source
    assert '_hist_results_by_binIndex.setdefault(_r.get("binIndex"), _r)' in source
    assert (
        '(_hist_results_by_binIndex[i].get("Count", 0) if i in _hist_results_by_binIndex '
        "else -1)" in source
    )
    # the index is inserted immediately before the outer loop, at its indentation
    lines = source.splitlines()
    loop = lines.index("    for i in range(bins):")
    assert lines[loop - 3 : loop] == [
        "    _hist_results_by_binIndex = {}",
        "    for _r in hist_results:",
        '        _hist_results_by_binIndex.setdefault(_r.get("binIndex"), _r)',
    ]
    original = _original(tmp_path)
    rng = random.Random(1234)
    for _ in range(300):
        rows = _rows(rng, rng.randint(0, 25))
        bins = rng.randint(0, 9)
        assert patched["histogram"](rows, bins) == original["histogram"](rows, bins)
    assert any("First-match" in note for note in proposal.notes)
    assert any("hashable" in note for note in proposal.notes)
    assert any("Extra memory" in note for note in proposal.notes)


def test_reversed_comparison_converter_key_and_element_value(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def pick(rows: list[dict], wanted: list[str]) -> list[object]:
            out = []
            for w in wanted:
                match = next((r for r in rows if w == str(r["id"])), None)
                if match is not None and match.get("ok"):
                    out.append(match["name"])
                else:
                    out.append(len(out))
            return out
        """,
    )
    proposal = _propose(tmp_path)
    patched = _accepted(tmp_path, proposal)
    assert '.setdefault(str(_r["id"]), _r)' in proposal.patched_source
    original = _original(tmp_path)
    rng = random.Random(7)
    for _ in range(200):
        rows = [
            {"id": rng.choice([1, 2, 3, "1", "x"]), "name": f"n{k}", "ok": rng.random() < 0.7}
            for k in range(rng.randint(0, 12))
        ]
        wanted = [rng.choice(["1", "2", "3", "x", "y"]) for _ in range(rng.randint(0, 8))]
        assert patched["pick"](rows, wanted) == original["pick"](rows, wanted)
    assert any(
        "evaluated once for every element" in note and "raises" in note for note in proposal.notes
    )


def test_complex_probe_is_evaluated_once_through_a_temporary(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def totals(items: list[object], orders: tuple[object, ...], offset: int) -> list[int]:
            out = []
            for order in orders:
                found = next((i.price for i in items if i.sku == order.sku + offset), 0)
                out.append(found)
            return out
        """,
    )
    proposal = _propose(tmp_path)
    patched = _accepted(tmp_path, proposal)
    assert "(_probe := order.sku + offset)" in proposal.patched_source
    assert any("evaluated once per lookup" in note for note in proposal.notes)
    original = _original(tmp_path)
    rng = random.Random(3)
    for _ in range(200):
        items = [
            SimpleNamespace(sku=rng.randint(0, 8), price=rng.randint(1, 50))
            for _ in range(rng.randint(0, 15))
        ]
        orders = tuple(SimpleNamespace(sku=rng.randint(0, 8)) for _ in range(rng.randint(0, 6)))
        offset = rng.randint(-1, 1)
        assert patched["totals"](items, orders, offset) == original["totals"](items, orders, offset)


def test_any_lookup_becomes_a_key_set(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def missing(rows: list[dict], wanted: list[int]) -> list[int]:
            out = []
            for w in wanted:
                if not any(r["id"] == w for r in rows):
                    out.append(w)
            return out
        """,
    )
    proposal = _propose(tmp_path)
    assert proposal.transform == "any_lookup_to_key_set"
    patched = _accepted(tmp_path, proposal)
    assert '_rows_id_set = {_r["id"] for _r in rows}' in proposal.patched_source
    assert "if not (w in _rows_id_set):" in proposal.patched_source
    original = _original(tmp_path)
    rng = random.Random(11)
    for _ in range(200):
        rows = [{"id": rng.randint(0, 9)} for _ in range(rng.randint(0, 10))]
        wanted = [rng.randint(0, 12) for _ in range(rng.randint(0, 10))]
        assert patched["missing"](rows, wanted) == original["missing"](rows, wanted)


def test_index_is_hoisted_before_the_outermost_invariant_loop(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def grid(cells: list[dict], width: int, height: int) -> list[list[int]]:
            out = []
            for y in range(height):
                row = []
                for x in range(width):
                    row.append(next((c["v"] for c in cells if c["xy"] == (x, y)), 0))
                out.append(row)
            return out
        """,
    )
    proposal = _propose(tmp_path)
    patched = _accepted(tmp_path, proposal)
    lines = proposal.patched_source.splitlines()
    assert lines.index("    for y in range(height):") == lines.index("    _cells_by_xy = {}") + 3
    original = _original(tmp_path)
    rng = random.Random(5)
    for _ in range(100):
        cells = [
            {"xy": (rng.randint(0, 3), rng.randint(0, 3)), "v": rng.randint(1, 9)}
            for _ in range(rng.randint(0, 12))
        ]
        assert patched["grid"](cells, 4, 3) == original["grid"](cells, 4, 3)


def test_self_attribute_with_declared_collection_type(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        class Catalog:
            products: list[dict]

            def __init__(self, products: list[dict]) -> None:
                self.products = products

            def prices(self, skus: list[str]) -> list[float]:
                out = []
                for sku in skus:
                    out.append(next((p["price"] for p in self.products if p["sku"] == sku), 0.0))
                return out
        """,
    )
    proposal = _propose(tmp_path)
    patched = _accepted(tmp_path, proposal)
    assert "for _p in self.products:" in proposal.patched_source
    original = _original(tmp_path)
    rng = random.Random(9)
    for _ in range(100):
        products = [
            {"sku": rng.choice("abcd"), "price": float(rng.randint(1, 9))}
            for _ in range(rng.randint(0, 8))
        ]
        skus = [rng.choice("abcde") for _ in range(rng.randint(0, 6))]
        assert patched["Catalog"](products).prices(skus) == original["Catalog"](products).prices(
            skus
        )


def test_collection_type_is_proven_through_an_imported_async_function(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/data.py",
        """
        from typing import Any, Dict, List

        async def load(query: str) -> List[Dict[str, Any]]:
            return [{"k": i % 3, "v": i} for i in range(int(query))]
        """,
    )
    _write(
        tmp_path,
        "pkg/mod.py",
        """
        from pkg.data import load


        async def report(query: str, keys: list[int]) -> list[int]:
            rows = await load(query)
            out = []
            for k in keys:
                out.append(next((r["v"] for r in rows if r["k"] == k), -1))
            return out
        """,
    )
    proposal = _propose(tmp_path, "pkg/mod.py")
    _accepted(tmp_path, proposal, "pkg/mod.py", execute=False)
    assert any(
        "`load()` is annotated to return `List[Dict[str, Any]]`" in note for note in proposal.notes
    )
    patched_ns: dict[str, Any] = {}
    original_ns: dict[str, Any] = {}

    async def load(query: str) -> list[dict[str, int]]:
        return [{"k": i % 3, "v": i} for i in range(int(query))]

    for namespace, source in (
        (patched_ns, proposal.patched_source),
        (original_ns, (tmp_path / "pkg/mod.py").read_text()),
    ):
        namespace["__builtins__"] = __builtins__
        exec(compile(source.replace("from pkg.data import load", ""), "m", "exec"), namespace)
        namespace["load"] = load
    for query, keys in (("0", [0, 1]), ("5", [2, 0, 7, 2]), ("1", [])):
        assert asyncio.run(patched_ns["report"](query, keys)) == asyncio.run(
            original_ns["report"](query, keys)
        )


def test_unannotated_or_decorated_callee_is_not_trusted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pkg/data.py",
        """
        import functools

        def load():
            return iter([{"k": 1, "v": 2}])

        @functools.cache
        def cached() -> list[dict]:
            return [{"k": 1, "v": 2}]
        """,
    )
    for callee in ("load", "cached"):
        _write(
            tmp_path,
            "pkg/mod.py",
            f"""
            from pkg.data import {callee}


            def report(keys: list[int]) -> list[int]:
                rows = {callee}()
                out = []
                for k in keys:
                    out.append(next((r["v"] for r in rows if r["k"] == k), -1))
                return out
            """,
        )
        proposal = _propose(tmp_path, "pkg/mod.py")
        assert proposal.status == "no_safe_patch"
        assert f"line 5 assigns it from `{callee}()`" in (proposal.reason or "")


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next(r["v"] for r in rows if r["id"] == i))
                return out
            """,
            "has no default, so it raises StopIteration",
        ),
        (
            """
            def f(ids: list[int], load) -> list[object]:
                out = []
                for i in ids:
                    rows = load()
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                return out
            """,
            "`rows` is (re)bound inside the `for` loop at line 3 at line 4",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                    rows.append({"id": i, "v": 0})
                return out
            """,
            "line 5 calls `rows.append(...)`",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int], other: dict) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                    other["id"] = i
                return out
            """,
            "assigns or deletes a field named ['id'], which is part of the lookup key",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    match = next((r for r in rows if r["id"] == i), None)
                    if match is not None:
                        match["id"] = -1
                return out
            """,
            "assigns or deletes a field named ['id']",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    match = next((r for r in rows if r["id"] == i), None)
                    out.append(match)
                return out
            """,
            "`match` holds the matching item itself",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int], store) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                    store.update(out[-1])
                return out
            """,
            "calls `.update()` on `store`",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r["v"] for r in rows for s in r["subs"] if s == i), None))
                return out
            """,
            "has 2 `for` clauses",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r["id"] == i if r["ok"]), None))
                return out
            """,
            "has 2 `if` clauses",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r["id"] == i and r["ok"]), None))
                return out
            """,
            "must be a single `==` comparison; found an `and` of 2 conditions",
        ),
        (
            """
            def f(rows: list[object], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.key() == i), None))
                return out
            """,
            "calls `r.key()`",
        ),
        (
            """
            def f(rows: list[object], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if [r.a] == i), None))
                return out
            """,
            "builds a list/dict/set, which is unhashable",
        ),
        (
            """
            def f(rows: list[object], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.a == [i]), None))
                return out
            """,
            "is a list/dict/set, which is unhashable",
        ),
        (
            """
            def f(rows: list[object], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.a == lookup(i)), None))
                return out
            """,
            "contains a call to `lookup()`",
        ),
        (
            """
            def f(rows: list[object], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r.v for r in rows if r.a == i), make_default()))
                return out
            """,
            "is evaluated by next() before searching",
        ),
        (
            """
            def f(rows: list[object], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append((lambda: next((r for r in rows if r.a == i), None))())
                return out
            """,
            "inside a lambda",
        ),
        (
            """
            def f(rows: list[object], ids: list[list[int]]) -> list[object]:
                return [
                    [next((r for r in rows if r.a == i), None) for i in group] for group in ids
                ]
            """,
            "inside a comprehension",
        ),
        (
            """
            def f(rows, ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.a == i), None))
                return out
            """,
            "cannot prove parameter `rows` is a re-iterable collection",
        ),
        (
            """
            def f(rows: "Iterable[object]", ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.a == i), None))
                return out
            """,
            'is annotated `"Iterable[object]"`',
        ),
        (
            """
            def f(source: list[object], ids: list[int]) -> list[object]:
                rows = (r for r in source)
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.a == i), None))
                return out
            """,
            "a generator expression (a one-shot iterator)",
        ),
        (
            """
            ROWS = [1, 2]

            def f(ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r for r in ROWS if r.a == i), None))
                return out
            """,
            "`ROWS` is not a parameter or local variable",
        ),
        (
            """
            def f(flag: bool, ids: list[int]) -> list[object]:
                if flag:
                    rows = [1, 2]
                out = []
                for i in ids:
                    out.append(next((r for r in rows if r.a == i), None))
                return out
            """,
            "is not assigned on every path before",
        ),
        (
            """
            async def f(rows: list[dict], ids: list[int], sink) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                    await sink(i)
                return out
            """,
            "can pause (`await` at line 5)",
        ),
        (
            """
            async def f(rows: list[dict], ids) -> list[object]:
                out = []
                async for i in ids:
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                return out
            """,
            "can pause (`async for` at line 3)",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    try:
                        out.append(next((r["v"] for r in rows if r["id"] == i), None))
                    except KeyError:
                        out.append(None)
                return out
            """,
            'runs inside `try` (line 4) and its key `r["id"]` can raise',
        ),
        (
            """
            def next(it, default=None):
                return default

            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(next((r["v"] for r in rows if r["id"] == i), None))
                return out
            """,
            "is not the builtin `next()`",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                return next((r["v"] for r in rows if r["id"] == ids[0]), None)
            """,
            "is not inside a `for`, `async for` or `while` loop",
        ),
        (
            """
            def f(rows: list[int], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append(any(r == i for r in rows))
                return out
            """,
            "compares the whole item",
        ),
        (
            """
            def f(rows: list[dict], ids: list[int]) -> list[object]:
                out = []
                for i in ids:
                    out.append([r for r in rows if r["id"] == i])
                return out
            """,
            "found a comprehension that collects every match at line 4",
        ),
    ],
)
def test_unsafe_lookups_are_refused_with_a_precise_reason(
    tmp_path: Path, source: str, reason: str
) -> None:
    source = textwrap.dedent(source).lstrip()
    (tmp_path / "mod.py").write_text(source)
    tree = ast.parse(source)
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == "f"
    )
    proposal = generate_patch(
        PatchTarget(
            source_root=tmp_path,
            target_file="mod.py",
            target_function="f",
            line_start=function.lineno,
            line_end=function.end_lineno or function.lineno,
            rule_id="PP-LOOP-002",
        )
    )
    assert proposal.status == "no_safe_patch"
    assert proposal.reason is not None
    assert reason in proposal.reason, proposal.reason


def test_self_join_over_the_same_collection(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def parents(nodes: list[dict]) -> list[object]:
            out = []
            for node in nodes:
                out.append(next((p["name"] for p in nodes if p["id"] == node["parent"]), None))
            return out
        """,
    )
    proposal = _propose(tmp_path)
    patched = _accepted(tmp_path, proposal)
    original = _original(tmp_path)
    rng = random.Random(21)
    for _ in range(200):
        nodes = [
            {"id": rng.randint(0, 5), "parent": rng.randint(0, 6), "name": f"n{k}"}
            for k in range(rng.randint(0, 9))
        ]
        assert patched["parents"](nodes) == original["parents"](nodes)


def test_read_only_use_of_an_aliasing_result_is_accepted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def names(rows: list[dict], ids: list[int]) -> list[str]:
            out = []
            for i in ids:
                match = next((r for r in rows if r["id"] == i), None)
                if match is not None and match.get("name"):
                    out.append(str(match["name"]))
            return out
        """,
    )
    proposal = _propose(tmp_path)
    patched = _accepted(tmp_path, proposal)
    original = _original(tmp_path)
    rows = [{"id": 1, "name": "a"}, {"id": 1, "name": "b"}, {"id": 2}, {"id": 3, "name": "c"}]
    assert patched["names"](rows, [1, 2, 3, 4, 1]) == original["names"](rows, [1, 2, 3, 4, 1])


def test_neither_shape_reports_both_expectations(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mod.py",
        """
        def f(rows: list[dict]) -> int:
            return len(rows)
        """,
    )
    proposal = _propose(tmp_path, from_rule=False)
    assert proposal.status == "no_safe_patch"
    reason = proposal.reason or ""
    assert "expected a `for` loop nested inside another loop" in reason
    assert "expected a `next((... for x in <collection>" in reason
