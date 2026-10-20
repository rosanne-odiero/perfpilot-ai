"""Benchmark a workload's baseline tree against a candidate tree and persist every sample."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.benchmarking.candidates import (
    CandidateSource,
    has_candidate_resolver,
    resolve_candidate,
)
from backend.benchmarking.environment import capture_environment
from backend.benchmarking.harness import BenchmarkPlan, PairedBenchmarkResult, run_paired_benchmark
from backend.core.config import Settings
from backend.core.exceptions import (
    NotConfiguredError,
    NotFoundError,
    PerfPilotError,
    ValidationFailedError,
)
from backend.infrastructure.artifact_store import ArtifactStore
from backend.jobs.service import enqueue_job
from backend.models.database import session_scope, utcnow
from backend.models.enums import Provenance
from backend.models.orm.platform import Job
from backend.models.orm.workloads import BenchmarkRun, BenchmarkSample, SourceSnapshot
from backend.models.workload_api import BenchmarkRunOut, BenchmarkSampleOut
from backend.models.workloads import (
    REFERENCE_CANDIDATE_LABEL,
    BenchmarkRequest,
    CandidateKind,
    CandidateRef,
    RunStatus,
    WorkloadSpec,
)
from backend.services.profiling import WorkloadExecutionError, run_dir_for
from backend.services.source_snapshots import create_snapshot
from backend.services.workloads import (
    build_spec,
    get_workload,
    resolve_size,
    validate_spec_targets,
)

ProgressFn = Callable[[str, float, str | None], None]
BENCHMARK_JOB_TYPE = "workload.benchmark"


def _candidate_request(run: BenchmarkRun) -> str | CandidateRef:
    if run.candidate_kind == CandidateKind.REFERENCE:
        return "reference"
    assert run.candidate_id is not None
    return CandidateRef(candidate_id=run.candidate_id)


# ------------------------------------------------------------------------------------------
# Submission
# ------------------------------------------------------------------------------------------


def create_benchmark_run(session: Session, request: BenchmarkRequest) -> BenchmarkRun:
    spec = build_spec(get_workload(session, request.workload_id))
    size = resolve_size(spec, request.size)
    if isinstance(request.candidate, CandidateRef):
        if not has_candidate_resolver():
            raise NotConfiguredError(
                "benchmarking validated patch candidates requires the patch-validation phase",
                details={"candidate_id": request.candidate.candidate_id},
            )
        kind, candidate_id, label = (
            CandidateKind.CANDIDATE,
            request.candidate.candidate_id,
            f"candidate {request.candidate.candidate_id}",
        )
    else:
        if not spec.reference_candidate:
            raise ValidationFailedError(f"workload {spec.slug!r} has no reference candidate")
        kind, candidate_id, label = CandidateKind.REFERENCE, None, REFERENCE_CANDIDATE_LABEL
    run = BenchmarkRun(
        workload_id=spec.id,
        status=RunStatus.PENDING,
        provenance=Provenance.MEASURED,
        size=size,
        warmups=request.warmups,
        runs=request.runs,
        memory_runs=request.memory_runs,
        threshold_percent=request.threshold_percent,
        seed=request.seed,
        order=request.order,
        bootstrap_resamples=request.bootstrap_resamples,
        candidate_kind=kind,
        candidate_id=candidate_id,
        candidate_label=label,
    )
    session.add(run)
    session.flush()
    return run


def submit_benchmark(session: Session, request: BenchmarkRequest) -> tuple[BenchmarkRun, Job]:
    run = create_benchmark_run(session, request)
    spec = build_spec(get_workload(session, run.workload_id))
    processes = 2 + 2 * request.runs + 2 * request.memory_runs
    job = enqueue_job(
        session,
        BENCHMARK_JOB_TYPE,
        {"benchmark_run_id": run.id},
        timeout_seconds=spec.timeout_seconds * processes + 300,
        max_attempts=1,
        subject_type="benchmark_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


# ------------------------------------------------------------------------------------------
# Execution
# ------------------------------------------------------------------------------------------


def _plan(spec: WorkloadSpec, run: BenchmarkRun) -> BenchmarkPlan:
    return BenchmarkPlan(
        entrypoint=spec.entrypoint,
        fixture=spec.fixture_id,
        fixture_kwargs=spec.fixture_kwargs(run.size),
        invocation_mode=spec.invocation_mode,
        output_contract=spec.expected_output,
        timeout_seconds=spec.timeout_seconds,
        memory_limit_mb=spec.memory_limit_mb,
        permitted_env=spec.permitted_env_vars,
        warmups=run.warmups,
        runs=run.runs,
        memory_runs=run.memory_runs,
        seed=run.seed,
        order="alternate" if run.order == "alternate" else "random",
        bootstrap_resamples=run.bootstrap_resamples,
        threshold_percent=run.threshold_percent,
    )


def execute_benchmark_run(
    run_id: str, settings: Settings, progress: ProgressFn | None = None
) -> BenchmarkRun:
    """Execute a PENDING benchmark synchronously (used by the job handler and the CLI)."""
    candidate_dir = settings.workspaces_dir / "candidates" / run_id
    work_dir = run_dir_for(settings, f"benchmark-{run_id}")
    try:
        with session_scope() as session:
            run = _get_run(session, run_id)
            spec = build_spec(get_workload(session, run.workload_id))
            validate_spec_targets(spec)
            candidate = resolve_candidate(session, spec, _candidate_request(run), candidate_dir)
            baseline_root = Path(spec.workspace_path).resolve()
            baseline_snapshot = create_snapshot(
                session,
                workload_id=spec.id,
                role="baseline",
                label=f"benchmark {run_id}",
                root=baseline_root,
            )
            candidate_snapshot = create_snapshot(
                session,
                workload_id=spec.id,
                role=candidate.kind.value,
                label=candidate.label,
                root=candidate.root,
            )
            run.baseline_snapshot_id = baseline_snapshot.id
            run.candidate_snapshot_id = candidate_snapshot.id
            run.candidate_source_sha256 = candidate.source_sha256
            run.status = RunStatus.RUNNING
            run.started_at = utcnow()
            plan = _plan(spec, run)
            hashes = {
                "baseline_tree_sha256": baseline_snapshot.tree_sha256,
                "candidate_tree_sha256": candidate_snapshot.tree_sha256,
                "candidate_source_sha256": candidate.source_sha256,
            }
        result = run_paired_benchmark(
            plan,
            baseline_root=baseline_root,
            candidate_root=candidate.root,
            work_dir=work_dir,
            progress=progress,
        )
        environment = capture_environment(
            workload=spec.slug,
            size=plan.fixture_kwargs,
            size_name=run.size,
            mode="benchmark",
            settings={
                "warmups": plan.warmups,
                "runs": plan.runs,
                "memory_runs": plan.memory_runs,
                "order": plan.order,
                "seed": plan.seed,
                "threshold_percent": plan.threshold_percent,
                "bootstrap_resamples": plan.bootstrap_resamples,
                "timed_iterations_per_process": 1,
            },
            **hashes,
        )
        with session_scope() as session:
            _persist(session, settings, run_id, spec, candidate, result, environment)
        if result.failed:
            raise WorkloadExecutionError(
                result.error or "benchmark failed", details={"benchmark_run_id": run_id}
            )
        with session_scope() as session:
            return _get_run(session, run_id)
    except PerfPilotError as exc:
        _fail(run_id, exc.message, {"code": exc.code, **exc.details})
        raise
    except Exception as exc:
        _fail(run_id, f"{type(exc).__name__}: {exc}", {"code": "internal_error"})
        raise
    finally:
        shutil.rmtree(candidate_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)


def _persist(
    session: Session,
    settings: Settings,
    run_id: str,
    spec: WorkloadSpec,
    candidate: CandidateSource,
    result: PairedBenchmarkResult,
    environment: dict[str, Any],
) -> None:
    run = _get_run(session, run_id)
    for sample in result.samples:
        session.add(BenchmarkSample(benchmark_run_id=run_id, **sample.model_dump()))
    summary: dict[str, Any] = {
        "baseline": result.baseline.model_dump() if result.baseline else None,
        "candidate": result.candidate.model_dump() if result.candidate else None,
        "speedup": result.speedup,
        "cpu_speedup": result.cpu_speedup,
        "latency_reduction_percent": result.latency_reduction_percent,
        "throughput_speedup": result.throughput_speedup,
        "ci": result.ci.model_dump() if result.ci else None,
        "thresholds": {
            "threshold_percent": run.threshold_percent,
            "improvement_speedup": result.classification.improvement_speedup_threshold,
            "regression_speedup": result.classification.regression_speedup_threshold,
        },
        "formulas": {
            "speedup": "median_baseline / median_candidate (wall time)",
            "latency_reduction_percent": (
                "(median_baseline - median_candidate) / median_baseline * 100"
            ),
            "throughput_speedup": "candidate_throughput / baseline_throughput",
        },
        "unit": "ms",
        "provenance": Provenance.MEASURED.value,
    }
    store = ArtifactStore(settings)
    raw = {
        "benchmark_run_id": run_id,
        "workload": spec.slug,
        "candidate_label": candidate.label,
        "summary": summary,
        "classification": result.classification.model_dump(),
        "memory": result.memory,
        "samples": [s.model_dump() for s in result.samples],
    }
    artifact = store.save_bytes(
        session,
        kind="benchmark",
        filename=f"{spec.slug}-{run.size}-benchmark.json",
        data=json.dumps(raw, indent=1, default=str).encode("utf-8"),
        media_type="application/json",
        owner_type="benchmark_run",
        owner_id=run_id,
        meta={"format": "raw benchmark samples"},
    )
    run.results_artifact_id = artifact.id
    run.input_sha256 = result.input_sha256
    run.classification = result.classification.classification
    run.classification_reason = result.classification.reason
    run.speedup = result.speedup
    run.speedup_ci_low = result.ci.low if result.ci else None
    run.speedup_ci_high = result.ci.high if result.ci else None
    run.latency_reduction_percent = result.latency_reduction_percent
    run.summary = summary
    run.memory = result.memory
    run.correctness = result.correctness.model_dump() if result.correctness else {}
    run.environment = environment
    run.warnings = result.warnings
    run.status = RunStatus.FAILED if result.failed else RunStatus.SUCCEEDED
    run.error = result.error
    run.finished_at = utcnow()


def _fail(run_id: str, message: str, details: dict[str, Any]) -> None:
    with session_scope() as session:
        run = session.get(BenchmarkRun, run_id)
        if run is None:
            return
        run.status = RunStatus.FAILED
        run.error = message
        run.error_details = details
        run.finished_at = utcnow()


def _get_run(session: Session, run_id: str) -> BenchmarkRun:
    run = session.get(BenchmarkRun, run_id)
    if run is None:
        raise NotFoundError(f"benchmark {run_id} not found")
    return run


# ------------------------------------------------------------------------------------------
# Views (shared by API and CLI)
# ------------------------------------------------------------------------------------------


def _snapshot(session: Session, snapshot_id: str | None) -> dict[str, Any] | None:
    snapshot = session.get(SourceSnapshot, snapshot_id) if snapshot_id else None
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "role": snapshot.role,
        "label": snapshot.label,
        "tree_sha256": snapshot.tree_sha256,
        "files": snapshot.files,
    }


def benchmark_view(session: Session, run_id: str) -> BenchmarkRunOut:
    run = _get_run(session, run_id)
    return BenchmarkRunOut(
        id=run.id,
        workload_id=run.workload_id,
        job_id=run.job_id,
        status=run.status,
        provenance=Provenance(run.provenance),
        size=run.size,
        settings={
            "warmups": run.warmups,
            "runs": run.runs,
            "memory_runs": run.memory_runs,
            "threshold_percent": run.threshold_percent,
            "seed": run.seed,
            "order": run.order,
            "bootstrap_resamples": run.bootstrap_resamples,
        },
        candidate={
            "kind": run.candidate_kind,
            "candidate_id": run.candidate_id,
            "label": run.candidate_label,
            "source_sha256": run.candidate_source_sha256,
        },
        baseline_snapshot=_snapshot(session, run.baseline_snapshot_id),
        candidate_snapshot=_snapshot(session, run.candidate_snapshot_id),
        input_sha256=run.input_sha256,
        classification=run.classification,
        classification_reason=run.classification_reason,
        speedup=run.speedup,
        speedup_ci_low=run.speedup_ci_low,
        speedup_ci_high=run.speedup_ci_high,
        latency_reduction_percent=run.latency_reduction_percent,
        summary=run.summary,
        memory=run.memory,
        correctness=run.correctness,
        environment=run.environment,
        warnings=run.warnings,
        results_artifact_id=run.results_artifact_id,
        error=run.error,
        error_details=run.error_details,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def benchmark_samples(
    session: Session, run_id: str, *, kind: str | None = None
) -> list[BenchmarkSampleOut]:
    _get_run(session, run_id)
    stmt = select(BenchmarkSample).where(BenchmarkSample.benchmark_run_id == run_id)
    if kind:
        stmt = stmt.where(BenchmarkSample.kind == kind)
    rows = session.scalars(
        stmt.order_by(
            BenchmarkSample.kind, BenchmarkSample.pair_index, BenchmarkSample.position_in_pair
        )
    )
    return [BenchmarkSampleOut.model_validate(row) for row in rows]
