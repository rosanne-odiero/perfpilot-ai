"""Benchmark statistics and the formulas of base spec §12.3.

* ``Speedup = median_baseline / median_optimized``
* ``LatencyReductionPercent = (median_baseline - median_optimized) / median_baseline * 100``
* ``ThroughputSpeedup = optimized_throughput / baseline_throughput``

Note that the latency reduction is *not* ``100 - speedup`` (nor ``(speedup - 1) * 100``):
200 ms -> 125 ms is a 1.6x speedup and a 37.5% latency reduction.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel

P95_STABLE_MIN_SAMPLES = 100


class SampleStats(BaseModel):
    n: int
    median: float
    mean: float
    min: float
    max: float
    stdev: float | None
    p95: float
    cv_percent: float | None
    p95_warning: str | None = None


class BootstrapCI(BaseModel):
    low: float
    high: float
    confidence: float
    resamples: int
    seed: int
    method: str = "paired percentile bootstrap of the ratio of medians"


def describe(values: Sequence[float]) -> SampleStats:
    if not values:
        raise ValueError("cannot describe an empty sample")
    data = [float(v) for v in values]
    n = len(data)
    mean = statistics.fmean(data)
    stdev = statistics.stdev(data) if n >= 2 else None
    warning = None
    if n < P95_STABLE_MIN_SAMPLES:
        warning = (
            f"p95 estimated from only n={n} samples is unstable "
            f"(it depends on the {max(1, math.ceil(n * 0.05))} slowest run(s))"
        )
    return SampleStats(
        n=n,
        median=statistics.median(data),
        mean=mean,
        min=min(data),
        max=max(data),
        stdev=stdev,
        p95=float(np.percentile(np.asarray(data), 95)),
        cv_percent=(stdev / mean * 100.0) if stdev is not None and mean > 0 else None,
        p95_warning=warning,
    )


def speedup(median_baseline: float, median_optimized: float) -> float:
    if median_baseline <= 0 or median_optimized <= 0:
        raise ValueError("medians must be positive")
    return median_baseline / median_optimized


def latency_reduction_percent(median_baseline: float, median_optimized: float) -> float:
    if median_baseline <= 0:
        raise ValueError("baseline median must be positive")
    return (median_baseline - median_optimized) / median_baseline * 100.0


def throughput(items: float, seconds: float) -> float:
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return items / seconds


def throughput_speedup(optimized_throughput: float, baseline_throughput: float) -> float:
    if baseline_throughput <= 0:
        raise ValueError("baseline throughput must be positive")
    return optimized_throughput / baseline_throughput


def bootstrap_speedup_ci(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    resamples: int = 10_000,
    seed: int = 0,
    confidence: float = 0.95,
) -> BootstrapCI:
    """Percentile CI for ``median(baseline) / median(candidate)``.

    Samples are paired (pair *i* = the *i*-th baseline and candidate runs, executed back to
    back), so each resample draws pair indices with replacement and keeps pairs together.
    """
    if len(baseline) != len(candidate):
        raise ValueError("paired bootstrap needs equally many baseline and candidate samples")
    if len(baseline) < 2:
        raise ValueError("bootstrap needs at least 2 pairs")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    base = np.asarray(baseline, dtype=float)
    cand = np.asarray(candidate, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(base), size=(resamples, len(base)))
    ratios = np.median(base[idx], axis=1) / np.median(cand[idx], axis=1)
    alpha = (1.0 - confidence) / 2.0
    low, high = np.quantile(ratios, [alpha, 1.0 - alpha])
    return BootstrapCI(
        low=float(low), high=float(high), confidence=confidence, resamples=resamples, seed=seed
    )
