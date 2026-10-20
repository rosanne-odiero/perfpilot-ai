from __future__ import annotations

import pytest

from backend.benchmarking.regression_detector import ClassifierConfig, classify, thresholds
from backend.models.workloads import BenchmarkClassification as C

CONFIG = ClassifierConfig(threshold_percent=5.0)


def _classify(s: float, lo: float, hi: float, pairs: int = 20, **kw: object) -> C:
    return classify(
        speedup=s, ci_low=lo, ci_high=hi, pairs=pairs, config=CONFIG, **kw
    ).classification  # type: ignore[arg-type]


def test_thresholds_are_latency_based() -> None:
    improve, regress = thresholds(5.0)
    assert improve == pytest.approx(1 / 0.95)
    assert regress == pytest.approx(1 / 1.05)


def test_clear_improvement() -> None:
    assert _classify(1.6, 1.5, 1.7) == C.IMPROVED


def test_clear_regression() -> None:
    assert _classify(0.5, 0.45, 0.55) == C.REGRESSED


def test_tiny_difference_is_never_improved_even_with_tight_ci() -> None:
    assert _classify(1.001, 1.0005, 1.0015) == C.NO_CLEAR_DIFFERENCE
    assert _classify(0.999, 0.9985, 0.9995) == C.NO_CLEAR_DIFFERENCE


def test_no_difference_when_ci_includes_one_inside_band() -> None:
    assert _classify(1.01, 0.98, 1.03) == C.NO_CLEAR_DIFFERENCE


def test_point_above_threshold_but_ci_includes_one_is_inconclusive() -> None:
    assert _classify(1.10, 0.99, 1.20) == C.INCONCLUSIVE
    assert _classify(0.90, 0.85, 1.01) == C.INCONCLUSIVE


def test_ci_spanning_both_thresholds_is_inconclusive() -> None:
    assert _classify(1.0, 0.7, 1.4) == C.INCONCLUSIVE


def test_too_few_pairs_is_inconclusive() -> None:
    assert _classify(2.0, 1.9, 2.1, pairs=3) == C.INCONCLUSIVE


def test_invalid_reasons_win() -> None:
    result = classify(
        speedup=3.0,
        ci_low=2.9,
        ci_high=3.1,
        pairs=20,
        config=CONFIG,
        invalid_reasons=["candidate output is not equivalent"],
    )
    assert result.classification == C.INVALID
    assert "not equivalent" in result.reason


def test_missing_speedup_is_invalid() -> None:
    assert (
        classify(speedup=None, ci_low=None, ci_high=None, pairs=0, config=CONFIG).classification
        == C.INVALID
    )


def test_threshold_is_configurable() -> None:
    strict = ClassifierConfig(threshold_percent=1.0)
    result = classify(speedup=1.03, ci_low=1.02, ci_high=1.04, pairs=20, config=strict)
    assert result.classification == C.IMPROVED
    assert _classify(1.03, 1.02, 1.04) == C.NO_CLEAR_DIFFERENCE
