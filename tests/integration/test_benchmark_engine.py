"""Real paired A/B benchmarks (small sizes, few runs)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from backend.benchmarking.harness import BenchmarkPlan, run_paired_benchmark
from backend.models.workloads import (
    BenchmarkClassification,
    InvocationMode,
    OutputComparison,
    OutputContract,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

SLOWER_LIST_LOOKUP = """
def _scan(known_ids, queries):
    found = []
    for query in queries:
        if query in known_ids:
            found.append(query)
    return found


def find_known(known_ids, queries):
    # Deliberately slower candidate: does the same work three times.
    result = _scan(known_ids, queries)
    for _ in range(2):
        result = _scan(known_ids, queries)
    return result
"""


def _plan(**overrides: object) -> BenchmarkPlan:
    data: dict[str, object] = {
        "entrypoint": "workload:find_known",
        "fixture": "fixtures:make_input",
        "fixture_kwargs": {"n_known": 600, "n_queries": 600, "seed": 3},
        "invocation_mode": InvocationMode.KEYWORD,
        "output_contract": OutputContract(type="list[int]"),
        "timeout_seconds": 60,
        "memory_limit_mb": 1024,
        "warmups": 1,
        "runs": 6,
        "memory_runs": 1,
        "seed": 11,
        "bootstrap_resamples": 2000,
    }
    data.update(overrides)
    return BenchmarkPlan.model_validate(data)


def _candidate(tmp_path: Path, slug: str, workload_source: str | None = None) -> Path:
    dest = tmp_path / f"{slug}-candidate"
    shutil.copytree(EXAMPLES / slug, dest, ignore=shutil.ignore_patterns("__pycache__"))
    source = workload_source or (EXAMPLES / slug / "reference" / "optimized.py").read_text()
    (dest / "workload.py").write_text(source)
    return dest


def test_reference_beats_original_and_every_sample_is_kept(tmp_path: Path) -> None:
    result = run_paired_benchmark(
        _plan(),
        baseline_root=EXAMPLES / "list_lookup",
        candidate_root=_candidate(tmp_path, "list_lookup"),
        work_dir=tmp_path / "work",
    )
    assert not result.failed
    assert result.classification.classification == BenchmarkClassification.IMPROVED
    assert result.speedup is not None
    assert result.speedup > 2
    assert result.ci is not None
    assert result.ci.low > 1
    assert result.correctness is not None
    assert result.correctness.equivalent
    timing = [s for s in result.samples if s.kind == "timing"]
    assert len(timing) == 12
    assert {s.input_sha256 for s in result.samples} == {result.input_sha256}
    pairs = {s.pair_index for s in timing}
    assert pairs == set(range(6))
    assert {s.position_in_pair for s in timing if s.variant == "baseline"} <= {0, 1}
    assert result.memory["baseline"]["runs"] == 1
    assert result.memory["candidate"]["tracemalloc_peak_bytes_median"] is not None
    assert not (tmp_path / "work").exists()


def test_deliberately_slower_candidate_is_regressed(tmp_path: Path) -> None:
    result = run_paired_benchmark(
        _plan(runs=8, memory_runs=0, order="alternate"),
        baseline_root=EXAMPLES / "list_lookup",
        candidate_root=_candidate(tmp_path, "list_lookup", SLOWER_LIST_LOOKUP),
        work_dir=tmp_path / "work",
    )
    assert result.classification.classification == BenchmarkClassification.REGRESSED, (
        result.classification.reason
    )
    assert result.speedup is not None
    assert result.speedup < 0.8
    assert result.latency_reduction_percent is not None
    assert result.latency_reduction_percent < 0
    orders = [s.variant for s in result.samples if s.kind == "timing" and s.position_in_pair == 0]
    assert orders == ["baseline", "candidate"] * 4


def test_identical_candidate_is_never_improved(tmp_path: Path) -> None:
    same = _candidate(
        tmp_path, "list_lookup", (EXAMPLES / "list_lookup" / "workload.py").read_text()
    )
    result = run_paired_benchmark(
        _plan(runs=8, memory_runs=0),
        baseline_root=EXAMPLES / "list_lookup",
        candidate_root=same,
        work_dir=tmp_path / "work",
    )
    assert result.classification.classification in {
        BenchmarkClassification.NO_CLEAR_DIFFERENCE,
        BenchmarkClassification.INCONCLUSIVE,
    }


def test_wrong_output_is_invalid_and_stops_early(tmp_path: Path) -> None:
    wrong = _candidate(
        tmp_path, "list_lookup", "def find_known(known_ids, queries):\n    return []\n"
    )
    result = run_paired_benchmark(
        _plan(),
        baseline_root=EXAMPLES / "list_lookup",
        candidate_root=wrong,
        work_dir=tmp_path / "work",
    )
    assert result.classification.classification == BenchmarkClassification.INVALID
    assert "not equivalent" in result.classification.reason
    assert result.speedup is None
    assert all(s.kind == "preflight" for s in result.samples)


def test_different_inputs_are_refused(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path, "list_lookup")
    fixtures = candidate / "fixtures.py"
    fixtures.write_text(
        fixtures.read_text().replace("rng = random.Random(seed)", "rng = random.Random(seed + 1)")
    )
    result = run_paired_benchmark(
        _plan(),
        baseline_root=EXAMPLES / "list_lookup",
        candidate_root=candidate,
        work_dir=tmp_path / "work",
    )
    assert result.classification.classification == BenchmarkClassification.INVALID
    assert "different inputs" in result.classification.reason


def test_crashing_candidate_fails_the_benchmark(tmp_path: Path) -> None:
    crashing = _candidate(
        tmp_path,
        "list_lookup",
        "def find_known(known_ids, queries):\n    raise RuntimeError('x')\n",
    )
    result = run_paired_benchmark(
        _plan(),
        baseline_root=EXAMPLES / "list_lookup",
        candidate_root=crashing,
        work_dir=tmp_path / "work",
    )
    assert result.failed
    assert result.classification.classification == BenchmarkClassification.INVALID
    assert "RuntimeError" in (result.error or "")


def test_float_tolerance_contract_uses_values(tmp_path: Path) -> None:
    rounding = """
def customer_totals(customers, orders):
    totals = {}
    for order in reversed(orders):
        totals[order["customer_id"]] = totals.get(order["customer_id"], 0.0) + order["amount"]
    return [(c["name"], totals.get(c["id"], 0.0)) for c in customers]
"""
    plan = _plan(
        entrypoint="workload:customer_totals",
        fixture_kwargs={"n_customers": 30, "n_orders": 400, "seed": 5},
        output_contract=OutputContract(
            type="list", comparison=OutputComparison.FLOAT_TOLERANCE, rel_tol=1e-9, abs_tol=1e-9
        ),
        runs=3,
        memory_runs=0,
    )
    result = run_paired_benchmark(
        plan,
        baseline_root=EXAMPLES / "nested_loop",
        candidate_root=_candidate(tmp_path, "nested_loop", rounding),
        work_dir=tmp_path / "work",
    )
    assert result.correctness is not None
    assert result.correctness.equivalent, result.correctness.detail


@pytest.mark.slow
@pytest.mark.parametrize("slug", ["duplicate_counter", "string_builder", "regex_matching"])
def test_reference_candidates_at_small_size(tmp_path: Path, slug: str) -> None:
    from backend.services.workloads import load_manifest

    manifest = load_manifest(EXAMPLES / slug)
    plan = _plan(
        entrypoint=manifest.entrypoint,
        fixture_kwargs={**manifest.sizes["small"], "seed": manifest.seed},
        invocation_mode=manifest.invocation_mode,
        output_contract=manifest.expected_output,
        runs=10,
        warmups=2,
    )
    result = run_paired_benchmark(
        plan,
        baseline_root=EXAMPLES / slug,
        candidate_root=_candidate(tmp_path, slug),
        work_dir=tmp_path / "work",
    )
    assert not result.failed
    assert result.classification.classification != BenchmarkClassification.INVALID
