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


fixtures = _load("fixtures.py", "list_lookup_fixtures")
IMPLEMENTATIONS = {
    "original": _load("workload.py", "list_lookup_original"),
    "reference": _load("reference/optimized.py", "list_lookup_reference"),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModuleType:
    return IMPLEMENTATIONS[request.param]


def _expected(known: list[int], queries: list[int]) -> list[int]:
    return [q for q in queries if any(q == k for k in known)]


@pytest.mark.parametrize(
    ("known", "queries", "expected"),
    [
        ([], [], []),
        ([], [1, 2], []),
        ([1, 2], [], []),
        ([5], [5], [5]),
        ([5], [6], []),
        ([1, 2, 3], [3, 3, 1, 9], [3, 3, 1]),
        ([1, 1, 2], [1, 2, 2], [1, 2, 2]),
        ([-1, 0, 1], [0, -1, 2], [0, -1]),
    ],
)
def test_cases(impl: ModuleType, known: list[int], queries: list[int], expected: list[int]) -> None:
    assert impl.find_known(known, queries) == expected


def test_all_unique_all_hits(impl: ModuleType) -> None:
    known = list(range(100))
    assert impl.find_known(known, known[::-1]) == known[::-1]


@pytest.mark.parametrize("hit_ratio", [0.0, 0.1, 0.5, 1.0])
def test_generated_distributions(impl: ModuleType, hit_ratio: float) -> None:
    data = fixtures.make_input(200, 300, hit_ratio=hit_ratio, seed=7)
    result = impl.find_known(**data)
    assert result == _expected(data["known_ids"], data["queries"])
    if hit_ratio == 0.0:
        assert result == []
    if hit_ratio == 1.0:
        assert result == data["queries"]


def test_large_input_matches_set_semantics(impl: ModuleType) -> None:
    data = fixtures.make_input(3000, 3000, seed=11)
    known = set(data["known_ids"])
    assert impl.find_known(**data) == [q for q in data["queries"] if q in known]


def test_does_not_mutate_inputs(impl: ModuleType) -> None:
    data = fixtures.make_input(50, 50, seed=3)
    snapshot = {k: list(v) for k, v in data.items()}
    impl.find_known(**data)
    assert data == snapshot


def test_fixture_is_deterministic() -> None:
    assert fixtures.make_input(100, 100, seed=5) == fixtures.make_input(100, 100, seed=5)
    assert fixtures.make_input(100, 100, seed=5) != fixtures.make_input(100, 100, seed=6)
    data = fixtures.make_input(100, 10, seed=5)
    assert len(set(data["known_ids"])) == 100
