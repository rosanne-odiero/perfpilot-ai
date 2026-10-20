from __future__ import annotations

import importlib.util
import math
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


fixtures = _load("fixtures.py", "nested_loop_fixtures")
IMPLEMENTATIONS = {
    "original": _load("workload.py", "nested_loop_original"),
    "reference": _load("reference/optimized.py", "nested_loop_reference"),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModuleType:
    return IMPLEMENTATIONS[request.param]


def _assert_close(actual: list[tuple[str, float]], expected: list[tuple[str, float]]) -> None:
    assert [name for name, _ in actual] == [name for name, _ in expected]
    for (_, got), (_, want) in zip(actual, expected, strict=True):
        assert math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-9)


def test_empty(impl: ModuleType) -> None:
    assert impl.customer_totals([], []) == []
    assert impl.customer_totals([], [{"customer_id": 1, "amount": 2.0}]) == []
    assert impl.customer_totals([{"id": 1, "name": "a"}], []) == [("a", 0.0)]


def test_single_and_duplicates(impl: ModuleType) -> None:
    customers = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "a"}]
    orders = [
        {"customer_id": 1, "amount": 1.25},
        {"customer_id": 1, "amount": 2.5},
        {"customer_id": 3, "amount": 4.0},
        {"customer_id": 99, "amount": 100.0},
    ]
    assert impl.customer_totals(customers, orders) == [("a", 3.75), ("b", 0.0), ("a", 4.0)]


def test_float_accumulation_within_tolerance(impl: ModuleType) -> None:
    customers = [{"id": 7, "name": "x"}]
    orders = [{"customer_id": 7, "amount": 0.1} for _ in range(10)]
    _assert_close(impl.customer_totals(customers, orders), [("x", 1.0)])


@pytest.mark.parametrize("orphan_ratio", [0.0, 0.05, 0.5, 1.0])
def test_generated_distributions(impl: ModuleType, orphan_ratio: float) -> None:
    data = fixtures.make_input(40, 400, orphan_ratio=orphan_ratio, seed=12)
    expected = [
        (c["name"], math.fsum(o["amount"] for o in data["orders"] if o["customer_id"] == c["id"]))
        for c in data["customers"]
    ]
    _assert_close(impl.customer_totals(**data), expected)


def test_large_input_matches_original(impl: ModuleType) -> None:
    data = fixtures.make_input(300, 3000, seed=21)
    _assert_close(impl.customer_totals(**data), IMPLEMENTATIONS["original"].customer_totals(**data))


def test_fixture_is_deterministic() -> None:
    assert fixtures.make_input(10, 50, seed=1) == fixtures.make_input(10, 50, seed=1)
    assert fixtures.make_input(10, 50, seed=1) != fixtures.make_input(10, 50, seed=2)
