"""Classify a paired benchmark as IMPROVED / REGRESSED / NO_CLEAR_DIFFERENCE / INCONCLUSIVE /
INVALID.

Let ``t`` be the practical-significance threshold in percent of baseline latency (default 5).
A change counts as practically meaningful when the median latency moves by at least ``t``%:

* improvement band: ``speedup >= 1 / (1 - t/100)``   (latency reduced by >= t%)
* regression band:  ``speedup <= 1 / (1 + t/100)``   (latency increased by >= t%)

Rules, applied in order, with ``[lo, hi]`` the bootstrap CI of the speedup:

1. INVALID       - correctness failed, inputs differ, or runs failed.
2. INCONCLUSIVE  - fewer than ``min_pairs`` pairs, or the CI spans both a meaningful regression
                   and a meaningful improvement (too noisy to tell).
3. IMPROVED      - ``lo > 1`` (statistically faster) and the point speedup is in the
                   improvement band.
4. REGRESSED     - ``hi < 1`` (statistically slower) and the point speedup is in the
                   regression band.
5. INCONCLUSIVE  - the point estimate is beyond a threshold but the CI still includes 1.
6. NO_CLEAR_DIFFERENCE - otherwise: either not statistically distinguishable, or a real but
                   practically negligible difference (e.g. 0.1%).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from backend.models.workloads import BenchmarkClassification


class ClassifierConfig(BaseModel):
    threshold_percent: float = Field(default=5.0, gt=0, lt=100)
    min_pairs: int = Field(default=5, ge=2)


class Classification(BaseModel):
    classification: BenchmarkClassification
    reason: str
    improvement_speedup_threshold: float
    regression_speedup_threshold: float


def thresholds(threshold_percent: float) -> tuple[float, float]:
    """Return ``(improvement_speedup, regression_speedup)`` bounds for a latency threshold."""
    fraction = threshold_percent / 100.0
    return 1.0 / (1.0 - fraction), 1.0 / (1.0 + fraction)


def classify(
    *,
    speedup: float | None,
    ci_low: float | None,
    ci_high: float | None,
    pairs: int,
    config: ClassifierConfig,
    invalid_reasons: list[str] | None = None,
) -> Classification:
    improve, regress = thresholds(config.threshold_percent)

    def result(kind: BenchmarkClassification, reason: str) -> Classification:
        return Classification(
            classification=kind,
            reason=reason,
            improvement_speedup_threshold=improve,
            regression_speedup_threshold=regress,
        )

    if invalid_reasons:
        return result(BenchmarkClassification.INVALID, "; ".join(invalid_reasons))
    if speedup is None or ci_low is None or ci_high is None:
        return result(BenchmarkClassification.INVALID, "no speedup could be computed")
    if pairs < config.min_pairs:
        return result(
            BenchmarkClassification.INCONCLUSIVE,
            f"only {pairs} paired runs (need at least {config.min_pairs})",
        )
    t = config.threshold_percent
    ci = f"95% CI [{ci_low:.3f}x, {ci_high:.3f}x]"
    if ci_low < regress and ci_high > improve:
        return result(
            BenchmarkClassification.INCONCLUSIVE,
            f"{ci} spans both a >= {t:g}% regression and a >= {t:g}% improvement; too noisy",
        )
    if ci_low > 1.0 and speedup >= improve:
        return result(
            BenchmarkClassification.IMPROVED,
            f"speedup {speedup:.3f}x >= {improve:.3f}x ({t:g}% threshold) and {ci} excludes 1",
        )
    if ci_high < 1.0 and speedup <= regress:
        return result(
            BenchmarkClassification.REGRESSED,
            f"speedup {speedup:.3f}x <= {regress:.3f}x ({t:g}% threshold) and {ci} excludes 1",
        )
    if speedup >= improve or speedup <= regress:
        return result(
            BenchmarkClassification.INCONCLUSIVE,
            f"point speedup {speedup:.3f}x crosses the {t:g}% threshold but {ci} includes 1",
        )
    if ci_low > 1.0 or ci_high < 1.0:
        return result(
            BenchmarkClassification.NO_CLEAR_DIFFERENCE,
            f"difference is statistically detectable ({ci}) but below the {t:g}% "
            "practical threshold",
        )
    return result(
        BenchmarkClassification.NO_CLEAR_DIFFERENCE,
        f"{ci} includes 1 and the point speedup {speedup:.3f}x is within the {t:g}% threshold",
    )
