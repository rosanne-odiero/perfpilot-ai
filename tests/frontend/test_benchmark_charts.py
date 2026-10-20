from __future__ import annotations

from typing import Any

import pytest
from ui_fixtures import load

from frontend.components.benchmark_charts import (
    distribution_figure,
    memory_figure,
    memory_frame,
    noise_warnings,
    paired_ratios,
    ratio_figure,
    timing_frame,
)


@pytest.fixture
def run_and_samples() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = load("manifest.json")
    key = next(k for k in manifest if k.startswith("/benchmarks/") and k.endswith("/results"))
    return load(manifest[key.removesuffix("/results")]), load(manifest[key])


def test_timing_frame_uses_raw_successful_samples(
    run_and_samples: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    run, samples = run_and_samples
    frame = timing_frame(samples)
    timing = [s for s in samples if s["kind"] == "timing" and s["ok"]]
    assert len(frame) == len(timing)
    assert set(frame["variant"]) == {"baseline", "candidate"}
    baseline_median = frame.loc[frame["variant"] == "baseline", "wall_ms"].median()
    assert baseline_median == pytest.approx(run["summary"]["baseline"]["wall_ms"]["median"])


def test_paired_ratios_per_pair(
    run_and_samples: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    run, samples = run_and_samples
    ratios = paired_ratios(timing_frame(samples))
    assert len(ratios) == run["settings"]["runs"]
    assert ratios["pair_index"].is_monotonic_increasing
    assert ratios["ratio"].median() == pytest.approx(run["speedup"], rel=0.25)


def test_paired_ratios_handle_missing_variant() -> None:
    samples = [
        {
            "variant": "baseline",
            "kind": "timing",
            "pair_index": 0,
            "position_in_pair": 0,
            "wall_ns": 1_000_000,
            "cpu_ns": None,
            "ok": True,
        },
    ]
    assert paired_ratios(timing_frame(samples)).empty
    assert paired_ratios(timing_frame([])).empty


def test_failed_samples_are_excluded() -> None:
    samples = [
        {
            "variant": "baseline",
            "kind": "timing",
            "pair_index": 0,
            "position_in_pair": 0,
            "wall_ns": 2_000_000,
            "cpu_ns": 1_000_000,
            "ok": True,
        },
        {
            "variant": "candidate",
            "kind": "timing",
            "pair_index": 0,
            "position_in_pair": 1,
            "wall_ns": None,
            "cpu_ns": None,
            "ok": False,
            "error": "boom",
        },
    ]
    frame = timing_frame(samples)
    assert frame["variant"].tolist() == ["baseline"]
    assert frame["wall_ms"].tolist() == [2.0]


def test_figures_show_every_raw_sample(
    run_and_samples: tuple[dict[str, Any], list[dict[str, Any]]],
) -> None:
    run, samples = run_and_samples
    frame = timing_frame(samples)
    labels = {"baseline": "baseline", "candidate": run["candidate"]["label"]}
    dist = distribution_figure(frame, labels=labels, log_y=True)
    assert [t.type for t in dist.data] == ["box", "box"]
    assert all(t.boxpoints == "all" for t in dist.data)
    assert sum(len(t.y) for t in dist.data) == len(frame)
    assert dist.data[1].name == run["candidate"]["label"]
    assert dist.layout.yaxis.type == "log"
    ratio = ratio_figure(
        paired_ratios(frame),
        speedup=run["speedup"],
        ci=(run["speedup_ci_low"], run["speedup_ci_high"]),
    )
    assert len(ratio.data[0].y) == run["settings"]["runs"]
    memory = memory_frame(samples)
    assert len(memory) == 2 * run["settings"]["memory_runs"]
    mem_fig = memory_figure(memory, labels)
    assert {t.xaxis for t in mem_fig.data} == {"x", "x2"}


def test_noise_warnings_add_only_unreported_variation() -> None:
    noisy = {"wall_ms": {"cv_percent": 25.0}}
    run = {"warnings": ["p95 is unstable"], "summary": {"baseline": noisy, "candidate": noisy}}
    warnings = noise_warnings(run)
    assert warnings[0] == "p95 is unstable"
    assert sum("varies a lot" in w for w in warnings) == 2
    reported = {**run, "warnings": ["candidate wall time is noisy (CV 25%)"]}
    assert sum("varies a lot" in w for w in noise_warnings(reported)) == 1
    quiet = {"warnings": [], "summary": {"baseline": {"wall_ms": {"cv_percent": 1.0}}}}
    assert noise_warnings(quiet) == []
