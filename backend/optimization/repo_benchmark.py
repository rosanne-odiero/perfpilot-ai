"""Worker job ``repo_optimization.benchmark``: paired A/B benchmark of a VALIDATED candidate.

Both variants run the audit's approved workload command with the same harness request, image,
limits, environment, network (``none``) and dependencies; the only difference allowed between the
two mounted trees is the patched file (checked from content hashes before anything runs). Every
run is a fresh container. After ``warmups`` unmeasured pairs, ``runs`` measured pairs follow; the
order inside each pair is randomized with ``seed``.

What is timed: the harness runs the command *without* any profiler and reads
``time.perf_counter_ns`` (wall) and ``time.process_time_ns`` (CPU) immediately around it inside the
container. Container start-up, interpreter start-up and the harness's own imports are excluded.
Peak RSS is ``getrusage(RUSAGE_SELF).ru_maxrss`` of that process (interpreter included).
Statistics: :mod:`backend.benchmarking.statistics`; classification:
:mod:`backend.benchmarking.regression_detector`. All results are ``MEASURED``.
"""

from __future__ import annotations

import random
import shutil
import statistics
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from backend.benchmarking.regression_detector import ClassifierConfig, classify
from backend.benchmarking.statistics import (
    bootstrap_speedup_ci,
    describe,
    latency_reduction_percent,
    speedup,
)
from backend.core.config import Settings
from backend.core.exceptions import ConflictError, PerfPilotError
from backend.core.logging import get_logger
from backend.models.database import session_scope, utcnow
from backend.models.enums import Provenance
from backend.models.orm.repo_optimization import (
    RepoBenchmarkRun,
    RepoBenchmarkSample,
    RepoOptimizationCandidate,
)
from backend.models.workloads import BenchmarkClassification
from backend.optimization.repo_candidates import (
    TIMED_DESCRIPTION,
    advance,
    candidate_facts,
    ensure_patched_copy,
    get_candidate,
    snapshot_root,
)
from backend.optimization.repo_sandbox import (
    CommandScope,
    ContainerRun,
    environment_fingerprint,
    prepared_deps,
    require_backend,
    run_container,
)
from backend.optimization.repo_workspace import candidate_root
from backend.optimization.state_machine import OptimizationState
from backend.optimization.workspace import OriginalTreeChangedError, verify_original_unchanged
from backend.runtime.execution import harness_command
from backend.sandbox.backend import BackendStatus, ExecutionBackend, SandboxLimits
from backend.services.source_snapshots import hash_tree

log = get_logger(__name__)

ProgressFn = Callable[[str, float, str | None], None]
CancelCheck = Callable[[], None]
Variant = Literal["baseline", "candidate"]
NS_PER_MS = 1_000_000


class Step(BaseModel):
    warmup: bool
    pair_index: int
    variant: Variant


class Sample(BaseModel):
    step: Step
    sequence: int
    wall_ns: int | None
    cpu_ns: int | None
    peak_rss_bytes: int | None
    command_exit_code: int | None
    container_exit_code: int | None
    container_duration_s: float
    failure: str | None
    request_sha256: str


def plan_order(warmups: int, runs: int, seed: int) -> list[Step]:
    """Warm-up pairs, then measured pairs; the order inside each pair is a seeded coin flip."""
    rng = random.Random(seed)  # noqa: S311 - reproducible run order, not security
    steps: list[Step] = []
    for warmup, count in ((True, warmups), (False, runs)):
        for index in range(count):
            first: Variant = "baseline" if rng.random() < 0.5 else "candidate"
            second: Variant = "candidate" if first == "baseline" else "baseline"
            steps += [
                Step(warmup=warmup, pair_index=index, variant=first),
                Step(warmup=warmup, pair_index=index, variant=second),
            ]
    return steps


def timing_command(argv: list[str]) -> dict[str, Any]:
    command = harness_command(argv, trace_memory=False)
    command.update(stats_json=False, profile=False)
    return command


def _nonneg_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def to_sample(step: Step, sequence: int, run: ContainerRun) -> Sample:
    timing = run.section("timing")
    wall, cpu = _nonneg_int(timing.get("wall_ns")), _nonneg_int(timing.get("cpu_ns"))
    failure = run.failure()
    if failure is None and (wall is None or wall == 0 or timing.get("profiled") is not False):
        failure = "the harness reported no valid unprofiled timing"
    return Sample(
        step=step,
        sequence=sequence,
        wall_ns=wall,
        cpu_ns=cpu,
        peak_rss_bytes=_nonneg_int(run.section("process").get("ru_maxrss_bytes")),
        command_exit_code=run.command_exit_code,
        container_exit_code=run.result.exit_code,
        container_duration_s=run.result.duration_s,
        failure=failure,
        request_sha256=run.request_sha256,
    )


class InputCheck(BaseModel):
    identical: bool
    reasons: list[str]
    record: dict[str, Any]


def check_inputs(
    root: Path, patched_root: Path, target_file: str, deps: Path | None, scope: CommandScope
) -> InputCheck:
    """Refuse the comparison unless the two trees differ in exactly the patched file."""
    base_tree, base_files = hash_tree(root)
    cand_tree, cand_files = hash_tree(patched_root)
    differing = sorted(
        path
        for path in base_files.keys() | cand_files.keys()
        if base_files.get(path) != cand_files.get(path)
    )
    reasons = []
    if differing != [target_file]:
        reasons.append(
            f"the workspaces must differ only in {target_file}; they differ in "
            f"{', '.join(differing[:10]) or 'nothing'}"
        )
    shared = {
        "argv": scope.argv,
        "workdir": scope.workdir_rel,
        "image": scope.image,
        "limits": scope.limits.model_dump(),
        "deps_tree_sha256": hash_tree(deps)[0] if deps is not None else None,
    }
    return InputCheck(
        identical=not reasons,
        reasons=reasons,
        record={
            "baseline": {**shared, "workspace_tree_sha256": base_tree},
            "candidate": {**shared, "workspace_tree_sha256": cand_tree},
            "differing_files": differing,
            "patched_file": target_file,
        },
    )


class _Context(BaseModel):
    benchmark_id: str
    candidate_id: str
    snapshot_id: str
    target_file: str
    original_tree: str
    scope: CommandScope
    warmups: int
    runs: int
    seed: int
    threshold_percent: float
    resamples: int


def _load(settings: Settings, benchmark_id: str) -> tuple[_Context, Path]:
    with session_scope() as session:
        run = session.get(RepoBenchmarkRun, benchmark_id)
        if run is None:
            raise ConflictError(f"benchmark {benchmark_id} not found")
        if run.status != "QUEUED":
            raise ConflictError(f"benchmark {benchmark_id} is {run.status}, not QUEUED")
        candidate = get_candidate(session, run.candidate_id)
        run.status = "RUNNING"
        run.started_at = utcnow()
        context = _Context(
            benchmark_id=run.id,
            candidate_id=candidate.id,
            snapshot_id=candidate.snapshot_id,
            target_file=candidate.target_file,
            original_tree=candidate.original_tree_sha256,
            scope=CommandScope(
                approval_id=run.approval_id,
                argv=[str(a) for a in run.argv],
                workdir_rel=run.workdir_rel,
                image=run.image,
                limits=SandboxLimits.model_validate(run.limits),
            ),
            warmups=run.warmups,
            runs=run.runs,
            seed=run.seed,
            threshold_percent=run.threshold_percent,
            resamples=run.bootstrap_resamples,
        )
        root = snapshot_root(session, settings, candidate.snapshot_id)
    return context, root


def _record_sample(benchmark_id: str, sample: Sample) -> None:
    with session_scope() as session:
        session.add(
            RepoBenchmarkSample(
                benchmark_id=benchmark_id,
                variant=sample.step.variant,
                warmup=sample.step.warmup,
                pair_index=sample.step.pair_index,
                sequence=sample.sequence,
                wall_ns=sample.wall_ns,
                cpu_ns=sample.cpu_ns,
                peak_rss_bytes=sample.peak_rss_bytes,
                command_exit_code=sample.command_exit_code,
                container_exit_code=sample.container_exit_code,
                container_duration_s=sample.container_duration_s,
                ok=sample.failure is None,
                failure=sample.failure,
            )
        )


def _finish(
    ctx: _Context,
    *,
    status: str,
    candidate_to: OptimizationState,
    reason: str,
    fields: dict[str, Any],
    inputs_identical: bool = False,
) -> None:
    with session_scope() as session:
        run = session.get(RepoBenchmarkRun, ctx.benchmark_id)
        candidate = session.get(RepoOptimizationCandidate, ctx.candidate_id)
        assert run is not None
        assert candidate is not None
        run.status = status
        run.finished_at = utcnow()
        for key, value in fields.items():
            setattr(run, key, value)
        if OptimizationState(candidate.state) != OptimizationState.BENCHMARKING:
            return
        facts = candidate_facts(
            candidate,
            benchmark_status=status,
            benchmark_input_matches_baseline=inputs_identical,
        )
        if candidate_to == OptimizationState.COMPLETED:
            advance(candidate, candidate_to, facts, reason=reason)
            candidate.provenance = Provenance.MEASURED
        else:
            if candidate_to == OptimizationState.FAILED:
                candidate.error = reason
            advance(candidate, candidate_to, facts, reason=reason)


def execute_repo_benchmark(
    settings: Settings, benchmark_id: str, progress: ProgressFn, check_cancelled: CancelCheck
) -> dict[str, Any]:
    ctx, root = _load(settings, benchmark_id)
    scratch = candidate_root(settings, ctx.candidate_id) / "runs" / f"benchmark-{benchmark_id}"
    samples: list[Sample] = []
    try:
        backend, status = require_backend(settings)
        verify_original_unchanged(root, ctx.original_tree, "before benchmarking")
        patched_root = ensure_patched_copy(settings, ctx.candidate_id, root)
        deps = prepared_deps(settings, ctx.snapshot_id)
        inputs = check_inputs(root, patched_root, ctx.target_file, deps, ctx.scope)
        if not inputs.identical:
            reason = "refusing to compare: " + "; ".join(inputs.reasons)
            _finish(
                ctx,
                status="FAILED",
                candidate_to=OptimizationState.FAILED,
                reason=reason,
                fields={
                    "inputs": inputs.record,
                    "classification": BenchmarkClassification.INVALID.value,
                    "classification_reason": reason,
                    "error": reason,
                },
            )
            return {"benchmark_id": benchmark_id, "status": "FAILED", "reason": reason}
        command = timing_command(ctx.scope.argv)
        steps = plan_order(ctx.warmups, ctx.runs, ctx.seed)
        last: ContainerRun | None = None
        for sequence, step in enumerate(steps):
            check_cancelled()
            phase = "warm-up" if step.warmup else "measured"
            progress(
                "benchmarking",
                0.05 + 0.9 * sequence / len(steps),
                f"{phase} run {step.pair_index + 1} ({step.variant})",
            )
            last = run_container(
                backend,
                scope=ctx.scope,
                workspace=root if step.variant == "baseline" else patched_root,
                scratch=scratch / str(sequence),
                command=command,
                deps_dir=deps,
            )
            sample = to_sample(step, sequence, last)
            samples.append(sample)
            _record_sample(benchmark_id, sample)
            shutil.rmtree(scratch / str(sequence), ignore_errors=True)
            if sample.failure is not None:
                break
        verify_original_unchanged(root, ctx.original_tree, "after benchmarking")
        if hash_tree(patched_root)[0] != inputs.record["candidate"]["workspace_tree_sha256"]:
            raise ConflictError("the patched workspace changed while it was being benchmarked")
    except OriginalTreeChangedError as exc:
        _finish(
            ctx,
            status="FAILED",
            candidate_to=OptimizationState.FAILED,
            reason=exc.message,
            fields={"error": exc.message},
        )
        raise
    except PerfPilotError as exc:
        _finish(
            ctx,
            status="FAILED",
            candidate_to=OptimizationState.VALIDATED,
            reason=f"benchmark could not run (retry possible): {exc.message}",
            fields={"error": exc.message},
        )
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    assert last is not None
    return _evaluate(ctx, backend, status, inputs, samples, last)


def _ms(values: list[int]) -> list[float]:
    return [v / NS_PER_MS for v in values]


def _evaluate(
    ctx: _Context,
    backend: ExecutionBackend,
    status: BackendStatus,
    inputs: InputCheck,
    samples: list[Sample],
    last: ContainerRun,
) -> dict[str, Any]:
    environment = {
        **environment_fingerprint(backend, status, ctx.scope, last),
        "timed": TIMED_DESCRIPTION,
        "dependencies": "prepared /deps (read-only)"
        if inputs.record["baseline"]["deps_tree_sha256"]
        else "image only",
        "profiler": "none",
    }
    order = [
        {"sequence": s.sequence, "warmup": s.step.warmup, "pair": s.step.pair_index}
        | {"variant": s.step.variant}
        for s in samples
    ]
    failed = [s for s in samples if s.failure is not None]
    if failed:
        first = failed[0]
        reason = (
            f"{first.step.variant} run {first.sequence} failed: {first.failure}; the comparison is "
            "invalid"
        )
        crashed_baseline = first.step.variant == "baseline"
        _finish(
            ctx,
            status="FAILED",
            candidate_to=OptimizationState.VALIDATED
            if crashed_baseline
            else OptimizationState.FAILED,
            reason=reason
            + (" (the original failed too; retry possible)" if crashed_baseline else ""),
            fields={
                "inputs": inputs.record,
                "order": order,
                "environment": environment,
                "classification": BenchmarkClassification.INVALID.value,
                "classification_reason": reason,
                "error": reason,
            },
        )
        return {"benchmark_id": ctx.benchmark_id, "status": "FAILED", "reason": reason}

    measured = [s for s in samples if not s.step.warmup]
    by_variant: dict[str, list[Sample]] = {
        v: sorted((s for s in measured if s.step.variant == v), key=lambda s: s.step.pair_index)
        for v in ("baseline", "candidate")
    }
    base_ms = _ms([s.wall_ns or 0 for s in by_variant["baseline"]])
    cand_ms = _ms([s.wall_ns or 0 for s in by_variant["candidate"]])
    base_stats, cand_stats = describe(base_ms), describe(cand_ms)
    ratio = speedup(base_stats.median, cand_stats.median)
    reduction = latency_reduction_percent(base_stats.median, cand_stats.median)
    ci = bootstrap_speedup_ci(base_ms, cand_ms, resamples=ctx.resamples, seed=ctx.seed)
    request_hashes = {s.request_sha256 for s in samples}
    invalid = [] if len(request_hashes) == 1 else ["the harness requests differed between runs"]
    verdict = classify(
        speedup=ratio,
        ci_low=ci.low,
        ci_high=ci.high,
        pairs=len(base_ms),
        config=ClassifierConfig(threshold_percent=ctx.threshold_percent),
        invalid_reasons=invalid,
    )
    warnings = [w for w in (base_stats.p95_warning, cand_stats.p95_warning) if w]
    summary = {
        "pairs": len(base_ms),
        "baseline": _variant_summary(by_variant["baseline"], base_stats.model_dump()),
        "candidate": _variant_summary(by_variant["candidate"], cand_stats.model_dump()),
        "ci": ci.model_dump(),
        "classifier": {
            "threshold_percent": ctx.threshold_percent,
            "improvement_speedup_threshold": verdict.improvement_speedup_threshold,
            "regression_speedup_threshold": verdict.regression_speedup_threshold,
        },
        "request_sha256": sorted(request_hashes),
        "timed": TIMED_DESCRIPTION,
    }
    _finish(
        ctx,
        status="SUCCEEDED",
        candidate_to=OptimizationState.COMPLETED if not invalid else OptimizationState.FAILED,
        reason=f"benchmark {verdict.classification.value}: {verdict.reason}",
        inputs_identical=not invalid,
        fields={
            "inputs": inputs.record,
            "order": order,
            "summary": summary,
            "speedup": ratio,
            "speedup_ci_low": ci.low,
            "speedup_ci_high": ci.high,
            "latency_reduction_percent": reduction,
            "classification": verdict.classification.value,
            "classification_reason": verdict.reason,
            "environment": environment,
            "warnings": warnings,
        },
    )
    log.info(
        "repository candidate benchmarked",
        extra={
            "candidate_id": ctx.candidate_id,
            "classification": verdict.classification.value,
            "speedup": round(ratio, 4),
        },
    )
    return {
        "benchmark_id": ctx.benchmark_id,
        "status": "SUCCEEDED",
        "classification": verdict.classification.value,
        "speedup": ratio,
        "speedup_ci": [ci.low, ci.high],
        "median_baseline_ms": base_stats.median,
        "median_candidate_ms": cand_stats.median,
    }


def _variant_summary(samples: list[Sample], wall: dict[str, Any]) -> dict[str, Any]:
    cpu = [s.cpu_ns / NS_PER_MS for s in samples if s.cpu_ns is not None]
    rss = [s.peak_rss_bytes for s in samples if s.peak_rss_bytes is not None]
    container = [s.container_duration_s for s in samples]
    return {
        "wall_ms": wall,
        "cpu_ms": describe(cpu).model_dump() if cpu else None,
        "peak_rss_bytes": {"median": statistics.median(rss), "max": max(rss)} if rss else None,
        "container_duration_s_median (not timed; includes container start/stop)": (
            statistics.median(container) if container else None
        ),
    }
