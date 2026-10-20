from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = _load("fixtures.py", "string_builder_fixtures")
IMPLEMENTATIONS = {
    "original": _load("workload.py", "string_builder_original"),
    "reference": _load("reference/optimized.py", "string_builder_reference"),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModuleType:
    return IMPLEMENTATIONS[request.param]


def test_empty(impl: ModuleType) -> None:
    assert impl.render_report([]) == ""


def test_single_row_exact_layout(impl: ModuleType) -> None:
    assert impl.render_report([("apple", 3, 1.5)]) == "apple            3        1.50\n"


def test_rounding_and_long_names(impl: ModuleType) -> None:
    rows = [("averyveryverylongname", 12345, 0.005), ("b", 0, 2.675)]
    assert impl.render_report(rows) == (
        f"{'averyveryverylongname':<12}{12345:>6}{0.005:>12.2f}\n{'b':<12}{0:>6}{2.675:>12.2f}\n"
    )


def test_duplicate_rows(impl: ModuleType) -> None:
    line = impl.render_report([("x", 1, 1.0)])
    assert impl.render_report([("x", 1, 1.0)] * 3) == line * 3


@pytest.mark.parametrize("max_name_length", [1, 6, 12, 30])
def test_generated_distributions(impl: ModuleType, max_name_length: int) -> None:
    rows = fixtures.make_input(300, max_name_length=max_name_length, seed=8)
    report = impl.render_report(rows)
    lines = report.splitlines()
    assert len(lines) == len(rows)
    for line, (name, quantity, price) in zip(lines, rows, strict=True):
        assert line == f"{name:<12}{quantity:>6}{price:>12.2f}"


def test_large_input_matches_join(impl: ModuleType) -> None:
    rows = fixtures.make_input(20000, seed=5)
    expected = "".join(f"{n:<12}{q:>6}{p:>12.2f}\n" for n, q, p in rows)
    assert impl.render_report(rows) == expected


def test_fixture_is_deterministic() -> None:
    assert fixtures.make_input(50, seed=1) == fixtures.make_input(50, seed=1)
    assert fixtures.make_input(50, seed=1) != fixtures.make_input(50, seed=2)
