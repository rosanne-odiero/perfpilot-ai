from __future__ import annotations

from backend.benchmarking.correctness import compare_outputs
from backend.models.workloads import OutputComparison, OutputContract
from backend.profiling.harness_entry import canonical, canonical_digest

EXACT = OutputContract(type="any")
TOLERANT = OutputContract(
    type="list", comparison=OutputComparison.FLOAT_TOLERANCE, rel_tol=1e-9, abs_tol=1e-9
)


def _result(value: object) -> dict[str, object]:
    digest, canon, _ = canonical_digest(value)
    return {"output_sha256": digest, "output_value": canon, "output_preview": repr(value)}


def test_canonical_mirrors_python_equality() -> None:
    assert canonical_digest({"a": 1, "b": 2})[0] == canonical_digest({"b": 2, "a": 1})[0]
    assert canonical_digest({3, 1, 2})[0] == canonical_digest({1, 2, 3})[0]
    assert canonical_digest([1, 2])[0] != canonical_digest([2, 1])[0]
    assert canonical_digest((1, 2))[0] != canonical_digest([1, 2])[0]
    assert canonical(float("nan")) == {"__float__": "nan"}
    assert canonical_digest(object())[2] is False


def test_exact_comparison() -> None:
    assert compare_outputs(_result([1, 2]), _result([1, 2]), EXACT).equivalent
    result = compare_outputs(_result([1, 2]), _result([1, 3]), EXACT)
    assert not result.equivalent
    assert "differ" in result.detail


def test_float_tolerance_accepts_rounding_noise_only() -> None:
    base = [("a", 0.1 + 0.2), ("b", 1.0)]
    close = [("a", 0.3), ("b", 1.0)]
    far = [("a", 0.31), ("b", 1.0)]
    assert compare_outputs(_result(base), _result(close), TOLERANT).equivalent
    assert not compare_outputs(_result(base), _result(far), TOLERANT).equivalent
    assert not compare_outputs(_result(base), _result([("a", 0.3)]), TOLERANT).equivalent
    assert not compare_outputs(
        _result(base), _result([("x", 0.3), ("b", 1.0)]), TOLERANT
    ).equivalent


def test_float_tolerance_without_values_is_not_equivalent() -> None:
    a, b = _result([1.0]), _result([2.0])
    a.pop("output_value")
    assert not compare_outputs(a, b, TOLERANT).equivalent


def test_bool_and_int_are_not_conflated_under_tolerance() -> None:
    assert not compare_outputs(_result([True]), _result([1]), TOLERANT).equivalent
