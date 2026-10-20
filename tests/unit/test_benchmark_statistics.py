from __future__ import annotations

import math

import pytest

from backend.benchmarking.statistics import (
    bootstrap_speedup_ci,
    describe,
    latency_reduction_percent,
    speedup,
    throughput,
    throughput_speedup,
)


def test_spec_example_200ms_to_125ms() -> None:
    assert speedup(200.0, 125.0) == pytest.approx(1.6)
    assert latency_reduction_percent(200.0, 125.0) == pytest.approx(37.5)


def test_latency_reduction_is_not_derived_from_speedup() -> None:
    s = speedup(200.0, 125.0)
    reduction = latency_reduction_percent(200.0, 125.0)
    assert reduction != pytest.approx(100 - s)
    assert reduction != pytest.approx((s - 1) * 100)
    assert reduction == pytest.approx((1 - 1 / s) * 100)


def test_regression_gives_speedup_below_one_and_negative_reduction() -> None:
    assert speedup(100.0, 150.0) == pytest.approx(2 / 3)
    assert latency_reduction_percent(100.0, 150.0) == pytest.approx(-50.0)


def test_throughput_speedup_matches_speedup_for_identical_work() -> None:
    base = throughput(1000, 0.200)
    opt = throughput(1000, 0.125)
    assert base == pytest.approx(5000)
    assert throughput_speedup(opt, base) == pytest.approx(speedup(200.0, 125.0))


@pytest.mark.parametrize(
    ("fn", "args"),
    [
        (speedup, (0.0, 1.0)),
        (speedup, (1.0, 0.0)),
        (latency_reduction_percent, (0.0, 1.0)),
        (throughput, (1.0, 0.0)),
        (throughput_speedup, (1.0, 0.0)),
    ],
)
def test_formulas_reject_non_positive(fn: object, args: tuple[float, float]) -> None:
    with pytest.raises(ValueError):
        fn(*args)  # type: ignore[operator]


def test_describe_basic_statistics() -> None:
    stats = describe([1, 2, 3, 4, 100])
    assert stats.n == 5
    assert stats.median == 3
    assert stats.mean == pytest.approx(22)
    assert stats.min == 1
    assert stats.max == 100
    assert stats.stdev == pytest.approx(43.6176, rel=1e-4)
    assert stats.p95 == pytest.approx(80.8)
    assert stats.p95_warning is not None
    assert "n=5" in stats.p95_warning


def test_describe_single_value_and_large_sample() -> None:
    single = describe([5.0])
    assert single.stdev is None
    assert single.cv_percent is None
    assert describe(list(range(200))).p95_warning is None
    with pytest.raises(ValueError):
        describe([])


def test_bootstrap_is_deterministic_for_a_seed() -> None:
    base = [10.0, 11.0, 9.5, 10.2, 10.8, 9.9, 10.1, 10.4]
    cand = [5.0, 5.4, 4.9, 5.1, 5.3, 5.0, 5.2, 4.8]
    first = bootstrap_speedup_ci(base, cand, resamples=2000, seed=7)
    second = bootstrap_speedup_ci(base, cand, resamples=2000, seed=7)
    other = bootstrap_speedup_ci(base, cand, resamples=2000, seed=8)
    assert first == second
    assert (first.low, first.high) != (other.low, other.high)
    point = speedup(10.15, 5.05)
    assert first.low <= point <= first.high
    assert first.low > 1.8


def test_bootstrap_identical_samples_gives_degenerate_ci_at_one() -> None:
    values = [3.0, 3.1, 2.9, 3.0, 3.05]
    ci = bootstrap_speedup_ci(values, values, resamples=1000, seed=1)
    assert ci.low <= 1.0 <= ci.high


def test_bootstrap_keeps_pairs_together() -> None:
    # Large between-pair drift but a constant 2x ratio inside every pair: pairing keeps the
    # ratio of medians close to 2.
    base = [10.0 * (i + 1) for i in range(10)]
    cand = [b / 2 for b in base]
    ci = bootstrap_speedup_ci(base, cand, resamples=2000, seed=3)
    assert math.isclose(ci.low, 2.0, rel_tol=0.05)
    assert math.isclose(ci.high, 2.0, rel_tol=0.05)


def test_bootstrap_input_validation() -> None:
    with pytest.raises(ValueError):
        bootstrap_speedup_ci([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        bootstrap_speedup_ci([1.0], [1.0])
    with pytest.raises(ValueError):
        bootstrap_speedup_ci([1.0, 2.0], [1.0, 2.0], confidence=1.0)
