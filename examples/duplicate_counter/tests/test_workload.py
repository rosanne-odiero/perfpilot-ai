from __future__ import annotations

import importlib.util
from collections import Counter
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


fixtures = _load("fixtures.py", "duplicate_counter_fixtures")
IMPLEMENTATIONS = {
    "original": _load("workload.py", "duplicate_counter_original"),
    "reference": _load("reference/optimized.py", "duplicate_counter_reference"),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModuleType:
    return IMPLEMENTATIONS[request.param]


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        ([], {}),
        (["a"], {}),
        (["a", "b", "c"], {}),
        (["a", "a"], {"a": 2}),
        (["a", "b", "a", "c", "b", "a"], {"a": 3, "b": 2}),
        (["x"] * 5, {"x": 5}),
        (["", "", "A", "a"], {"": 2}),
    ],
)
def test_cases(impl: ModuleType, items: list[str], expected: dict[str, int]) -> None:
    assert impl.count_duplicates(items) == expected


def test_key_order_is_first_occurrence(impl: ModuleType) -> None:
    assert list(impl.count_duplicates(["b", "a", "b", "a", "c", "c"])) == ["b", "a", "c"]


@pytest.mark.parametrize(
    ("unique_ratio", "skew"), [(0.05, 0.0), (0.3, 1.0), (0.9, 2.0), (1.0, 0.0)]
)
def test_generated_distributions(impl: ModuleType, unique_ratio: float, skew: float) -> None:
    items = fixtures.make_input(600, unique_ratio=unique_ratio, skew=skew, seed=4)
    expected = {k: v for k, v in Counter(items).items() if v > 1}
    assert impl.count_duplicates(items) == expected


def test_large_input(impl: ModuleType) -> None:
    items = fixtures.make_input(4000, seed=1)
    result = impl.count_duplicates(items)
    assert sum(result.values()) + sum(1 for v in Counter(items).values() if v == 1) == len(items)


def test_does_not_mutate_input(impl: ModuleType) -> None:
    items = fixtures.make_input(100, seed=2)
    snapshot = list(items)
    impl.count_duplicates(items)
    assert items == snapshot


def test_fixture_is_deterministic() -> None:
    assert fixtures.make_input(300, seed=9) == fixtures.make_input(300, seed=9)
    assert fixtures.make_input(300, seed=9) != fixtures.make_input(300, seed=10)
    assert len(fixtures.make_input(300, seed=9)) == 300
