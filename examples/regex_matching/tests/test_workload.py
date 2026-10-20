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


fixtures = _load("fixtures.py", "regex_matching_fixtures")
IMPLEMENTATIONS = {
    "original": _load("workload.py", "regex_matching_original"),
    "reference": _load("reference/optimized.py", "regex_matching_reference"),
}


@pytest.fixture(params=sorted(IMPLEMENTATIONS))
def impl(request: pytest.FixtureRequest) -> ModuleType:
    return IMPLEMENTATIONS[request.param]


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([], []),
        ([""], []),
        (["no errors here"], []),
        (["ERR-0001"], ["0001"]),
        (["a ERR-1234 b ERR-9999"], ["1234", "9999"]),
        (["ERR-1234", "ERR-1234"], ["1234", "1234"]),
        (["ERR-12 XERR-1234 ERR-12345 ERR-abcd"], []),
        (["(ERR-4321)", "ERR-0000."], ["4321", "0000"]),
    ],
)
def test_cases(impl: ModuleType, lines: list[str], expected: list[str]) -> None:
    assert impl.extract_error_codes(lines) == expected


@pytest.mark.parametrize("error_ratio", [0.0, 0.2, 1.0])
def test_generated_distributions(impl: ModuleType, error_ratio: float) -> None:
    lines = fixtures.make_input(500, error_ratio=error_ratio, seed=6)
    codes = impl.extract_error_codes(lines)
    expected = [
        word[4:]
        for line in lines
        for word in line.split()
        if word.startswith("ERR-") and len(word) == 8 and word[4:].isdigit()
    ]
    assert codes == expected
    if error_ratio == 0.0:
        assert codes == []


def test_large_input(impl: ModuleType) -> None:
    lines = fixtures.make_input(20000, seed=2)
    codes = impl.extract_error_codes(lines)
    assert all(len(code) == 4 and code.isdigit() for code in codes)
    assert len(codes) > 1000


def test_fixture_is_deterministic() -> None:
    assert fixtures.make_input(100, seed=3) == fixtures.make_input(100, seed=3)
    assert fixtures.make_input(100, seed=3) != fixtures.make_input(100, seed=4)
