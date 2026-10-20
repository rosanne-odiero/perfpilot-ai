"""Profile a registered workload: run the harness, persist hotspots, memory, and artifacts."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.benchmarking.environment import capture_environment
from backend.core.config import Settings
from backend.core.exceptions import NotFoundError, PerfPilotError
from backend.core.observability import traced
from backend.core.redaction import redact_text
from backend.infrastructure.artifact_store import ArtifactStore
from backend.jobs.service import enqueue_job
from backend.models.database import session_scope, utcnow
from backend.models.enums import Provenance
from backend.models.orm.platform import Job
from backend.models.orm.workloads import Hotspot, ProfileRun, SourceSnapshot
from backend.models.workload_api import HotspotOut, ProfileRunOut
from backend.models.workloads import HotspotSort, ProfileRequest, RunStatus, WorkloadSpec
from backend.profiling.hotspots import (
    MAX_STORED_HOTSPOTS,
    HotspotRecord,
    parse_profile,
    rank_hotspots,
)
from backend.profiling.runner import HarnessRun, ResourceLimits, run_harness
from backend.profiling.sampling import detect_py_spy, py_spy_wrapper
from backend.services.source_snapshots import create_snapshot
from backend.services.workloads import build_spec, get_workload, resolve_size, validate_spec_targets

ProgressFn = Callable[[str, float, str | None], None]
PROFILE_JOB_TYPE = "workload.profile"
PROFILE_RSS_SAMPLE_INTERVAL_S = 0.01
TOP_USER_HOTSPOTS = 10


class WorkloadExecutionError(PerfPilotError):
    status_code = 422
    code = "workload_execution_failed"


def _noop(_stage: str, _fraction: float, _message: str | None = None) -> None:
    return None


def run_dir_for(settings: Settings, name: str) -> Path:
    return settings.resolved_data_dir / "runs" / name


def failure_details(run: HarnessRun) -> dict[str, Any]:
    """Diagnostics for a failed harness run. Captured streams are redacted before storage."""
    error = (run.result or {}).get("error") or {}
    return {
        "reason": run.failure_reason(),
        "returncode": run.returncode,
        "timed_out": run.timed_out,
        "memory_limit_exceeded": run.memory_limit_exceeded,
        "error_type": error.get("type"),
        "error_code": error.get("code"),
        "traceback": redact_text(error.get("traceback", "")).text[-4000:],
        "stderr_tail": redact_text(run.stderr[-4000:]).text,
        "stdout_tail": redact_text(run.stdout[-2000:]).text,
        "limits": run.limits.model_dump(),
    }


def limits_for(spec: WorkloadSpec) -> ResourceLimits:
    return ResourceLimits(
        timeout_seconds=spec.timeout_seconds, memory_limit_mb=spec.memory_limit_mb
    )


def base_request(spec: WorkloadSpec, size: str, workspace: Path | None = None) -> dict[str, Any]:
    return {
        "workspace_root": str((workspace or Path(spec.workspace_path)).resolve()),
        "entrypoint": spec.entrypoint,
        "fixture": spec.fixture_id,
        "fixture_kwargs": spec.fixture_kwargs(size),
        "invocation_mode": spec.invocation_mode.value,
    }


# ------------------------------------------------------------------------------------------
# Submission
# ------------------------------------------------------------------------------------------


def create_profile_run(session: Session, request: ProfileRequest) -> ProfileRun:
    spec = build_spec(get_workload(session, request.workload_id))
    run = ProfileRun(
        workload_id=spec.id,
        size=resolve_size(spec, request.size),
        enable_sampling=request.enable_sampling,
        status=RunStatus.PENDING,
        provenance=Provenance.MEASURED,
    )
    session.add(run)
    session.flush()
    return run


def submit_profile(session: Session, request: ProfileRequest) -> tuple[ProfileRun, Job]:
    run = create_profile_run(session, request)
    spec = build_spec(get_workload(session, run.workload_id))
    job = enqueue_job(
        session,
        PROFILE_JOB_TYPE,
        {"profile_run_id": run.id},
        timeout_seconds=spec.timeout_seconds * 3 + 120,
        max_attempts=1,
        subject_type="profile_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


# ------------------------------------------------------------------------------------------
# Execution
# ------------------------------------------------------------------------------------------


def _memory_section(run: HarnessRun) -> dict[str, Any]:
    result = run.result or {}
    traced = result.get("memory") or {}
    process = result.get("process") or {}
    unavailable = []
    if run.rss_peak_bytes is None:
        unavailable.append("rss_peak_bytes: RSS could not be sampled")
    if process.get("ru_maxrss_bytes") is None:
        unavailable.append("ru_maxrss_bytes: getrusage unavailable on this platform")
    return {
        "tracemalloc_peak_bytes": traced.get("tracemalloc_peak_bytes"),
        "tracemalloc_retained_bytes": traced.get("tracemalloc_retained_bytes"),
        "rss_peak_bytes": run.rss_peak_bytes,
        "rss_samples": run.rss_samples,
        "rss_sample_interval_ms": (
            run.rss_sample_interval_s * 1000 if run.rss_sample_interval_s else None
        ),
        "ru_maxrss_bytes": process.get("ru_maxrss_bytes"),
        "unavailable": unavailable,
        "definitions": {
            "tracemalloc_peak_bytes": (
                "peak Python-traced allocations during one workload call (separate call, input "
                "built before tracing). Not process memory."
            ),
            "rss_peak_bytes": (
                "peak resident set size of the child process tree sampled by the parent over "
                "the whole run (interpreter + input + profiled and traced calls)"
            ),
            "ru_maxrss_bytes": "kernel-reported maximum RSS of the child process (getrusage)",
        },
        "provenance": Provenance.MEASURED.value,
    }


def _run_sampler(
    spec: WorkloadSpec, size: str, run_dir: Path
) -> tuple[dict[str, Any], Path | None, list[str]]:
    status = detect_py_spy()
    info: dict[str, Any] = {"requested": True, "tool": "py-spy", **status.model_dump()}
    if not status.available or status.binary is None:
        return info, None, [f"sampling profiler skipped: {status.reason}"]
    output = run_dir / "pyspy.speedscope.json"
    run = run_harness(
        {**base_request(spec, size), "mode": "time", "warmups": 0, "iterations": 1},
        run_dir=run_dir / "pyspy",
        limits=limits_for(spec),
        permitted_env=spec.permitted_env_vars,
        rss_sample_interval=None,
        wrapper_argv=py_spy_wrapper(status.binary, output),
    )
    if not run.ok or not output.is_file():
        reason = run.failure_reason() if not run.ok else "py-spy produced no output"
        info["error"] = redact_text(f"{reason} {run.stderr[-500:]}").text
        return info, None, [f"sampling profiler failed: {reason}"]
    return info, output, []


@traced("workload.profile")
def execute_profile_run(
    run_id: str, settings: Settings, progress: ProgressFn | None = None
) -> ProfileRun:
    """Execute a PENDING profile run synchronously (used by the job handler and the CLI)."""
    report = progress or _noop
    run_dir = run_dir_for(settings, f"profile-{run_id}")
    try:
        with session_scope() as session:
            run = _get_run(session, run_id)
            spec = build_spec(get_workload(session, run.workload_id))
            validate_spec_targets(spec)
            snapshot = create_snapshot(
                session,
                workload_id=spec.id,
                role="baseline",
                label=f"profile {run_id}",
                root=Path(spec.workspace_path),
            )
            run.snapshot_id = snapshot.id
            run.status = RunStatus.RUNNING
            run.started_at = utcnow()
            size, sampling_requested = run.size, run.enable_sampling
            tree_sha = snapshot.tree_sha256

        report("profiling", 0.1, "running cProfile + tracemalloc in a child process")
        harness = run_harness(
            {**base_request(spec, size), "mode": "profile"},
            run_dir=run_dir,
            limits=limits_for(spec),
            permitted_env=spec.permitted_env_vars,
            rss_sample_interval=PROFILE_RSS_SAMPLE_INTERVAL_S,
        )
        if not harness.ok:
            raise WorkloadExecutionError(
                f"workload run failed: {harness.failure_reason()}",
                details=failure_details(harness),
            )
        report("parsing", 0.6, "normalizing pstats output")
        result = harness.result or {}
        prof_path = run_dir / "out" / result["profile"]["prof_file"]
        parsed = parse_profile(prof_path, Path(spec.workspace_path))
        warnings = _profile_warnings(harness)
        sampling: dict[str, Any] = {"requested": False}
        sampling_output: Path | None = None
        if sampling_requested:
            report("sampling", 0.7, "running py-spy (optional)")
            sampling, sampling_output, sampler_warnings = _run_sampler(spec, size, run_dir)
            warnings.extend(sampler_warnings)
        report("persisting", 0.9, "saving hotspots and artifacts")
        environment = capture_environment(
            workload=spec.slug,
            size=size,
            fixture_kwargs=spec.fixture_kwargs(size),
            source_tree_sha256=tree_sha,
            mode="profile",
        )
        with session_scope() as session:
            _persist_profile(
                session,
                settings,
                run_id,
                spec,
                harness,
                parsed.model_dump(mode="json"),
                prof_path,
                sampling,
                sampling_output,
                warnings,
                environment,
            )
            return _get_run(session, run_id)
    except PerfPilotError as exc:
        _fail(run_id, exc.message, {"code": exc.code, **exc.details})
        raise
    except Exception as exc:
        _fail(run_id, f"{type(exc).__name__}: {exc}", {"code": "internal_error"})
        raise
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _profile_warnings(harness: HarnessRun) -> list[str]:
    warnings = [
        "cProfile adds per-call overhead: absolute times are inflated, and functions with many "
        "small calls are over-represented. Use the benchmark for real timings.",
    ]
    for name, reason in harness.limits.unsupported.items():
        warnings.append(f"resource limit {name} not applied: {reason}")
    if harness.stdout_truncated or harness.stderr_truncated:
        warnings.append("workload output was truncated")
    return warnings


def _persist_profile(
    session: Session,
    settings: Settings,
    run_id: str,
    spec: WorkloadSpec,
    harness: HarnessRun,
    parsed: dict[str, Any],
    prof_path: Path,
    sampling: dict[str, Any],
    sampling_output: Path | None,
    warnings: list[str],
    environment: dict[str, Any],
) -> None:
    run = _get_run(session, run_id)
    result = harness.result or {}
    store = ArtifactStore(settings)
    prof = store.save_file(
        session,
        kind="profile",
        source=prof_path,
        filename=f"{spec.slug}-{run.size}.prof",
        owner_type="profile_run",
        owner_id=run_id,
        meta={"format": "cProfile/pstats"},
    )
    normalized = store.save_bytes(
        session,
        kind="profile",
        filename=f"{spec.slug}-{run.size}-hotspots.json",
        data=json.dumps(parsed, indent=1).encode("utf-8"),
        media_type="application/json",
        owner_type="profile_run",
        owner_id=run_id,
        meta={"format": "normalized hotspots"},
    )
    run.prof_artifact_id, run.json_artifact_id = prof.id, normalized.id
    if sampling_output is not None:
        sampled = store.save_file(
            session,
            kind="profile",
            source=sampling_output,
            filename=f"{spec.slug}-{run.size}.speedscope.json",
            media_type="application/json",
            owner_type="profile_run",
            owner_id=run_id,
            meta={"format": "speedscope"},
        )
        run.sampling_artifact_id = sampled.id
        sampling["artifact_id"] = sampled.id
    hotspots = [HotspotRecord.model_validate(h) for h in parsed["hotspots"]]
    for record in hotspots[:MAX_STORED_HOTSPOTS]:
        session.add(Hotspot(profile_run_id=run_id, **record.model_dump(mode="json")))
    if len(hotspots) > MAX_STORED_HOTSPOTS:
        warnings.append(
            f"{len(hotspots)} functions profiled; only the top {MAX_STORED_HOTSPOTS} by exclusive "
            "time are stored as rows (all are in the normalized JSON artifact)"
        )
    profile = result["profile"]
    run.input_sha256 = result.get("input_sha256")
    run.total_profiled_time_ms = parsed["total_profiled_time_ms"]
    run.wall_time_ms = profile["wall_ns_under_profiler"] / 1e6
    run.metrics = {
        "total_profiled_time_ms": parsed["total_profiled_time_ms"],
        "wall_time_under_profiler_ms": profile["wall_ns_under_profiler"] / 1e6,
        "cpu_time_under_profiler_ms": profile["cpu_ns_under_profiler"] / 1e6,
        "function_count": parsed["function_count"],
        "user_function_count": sum(1 for h in hotspots if h.is_user_code),
        "output_sha256": result.get("output_sha256"),
        "output_preview": redact_text(result.get("output_preview", "")).text,
        "child_process_wall_seconds": harness.wall_seconds,
        "limits": harness.limits.model_dump(),
        "provenance": Provenance.MEASURED.value,
    }
    run.memory = _memory_section(harness)
    run.sampling = sampling
    run.environment = environment
    run.warnings = warnings
    run.status = RunStatus.SUCCEEDED
    run.finished_at = utcnow()


def _fail(run_id: str, message: str, details: dict[str, Any]) -> None:
    with session_scope() as session:
        run = session.get(ProfileRun, run_id)
        if run is None:
            return
        run.status = RunStatus.FAILED
        run.error = message
        run.error_details = details
        run.finished_at = utcnow()


def _get_run(session: Session, run_id: str) -> ProfileRun:
    run = session.get(ProfileRun, run_id)
    if run is None:
        raise NotFoundError(f"profile {run_id} not found")
    return run


# ------------------------------------------------------------------------------------------
# Views (shared by API and CLI)
# ------------------------------------------------------------------------------------------


def _snapshot_view(session: Session, snapshot_id: str | None) -> dict[str, Any] | None:
    if snapshot_id is None:
        return None
    snapshot = session.get(SourceSnapshot, snapshot_id)
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "role": snapshot.role,
        "tree_sha256": snapshot.tree_sha256,
        "files": snapshot.files,
    }


def list_hotspots(
    session: Session,
    run_id: str,
    *,
    sort: HotspotSort = HotspotSort.EXCLUSIVE,
    user_only: bool = False,
    limit: int | None = None,
) -> list[HotspotOut]:
    _get_run(session, run_id)
    rows = session.scalars(select(Hotspot).where(Hotspot.profile_run_id == run_id)).all()
    records = [HotspotRecord.model_validate(row, from_attributes=True) for row in rows]
    ranked = rank_hotspots(records, sort, user_only=user_only, limit=limit)
    return [HotspotOut.model_validate(r.model_dump()) for r in ranked]


def profile_view(session: Session, run_id: str) -> ProfileRunOut:
    run = _get_run(session, run_id)
    return ProfileRunOut(
        id=run.id,
        workload_id=run.workload_id,
        job_id=run.job_id,
        status=run.status,
        provenance=Provenance(run.provenance),
        size=run.size,
        enable_sampling=run.enable_sampling,
        input_sha256=run.input_sha256,
        total_profiled_time_ms=run.total_profiled_time_ms,
        wall_time_ms=run.wall_time_ms,
        metrics=run.metrics,
        memory=run.memory,
        sampling=run.sampling,
        environment=run.environment,
        warnings=run.warnings,
        top_user_hotspots=list_hotspots(session, run_id, user_only=True, limit=TOP_USER_HOTSPOTS),
        artifacts={
            "prof": run.prof_artifact_id,
            "hotspots_json": run.json_artifact_id,
            "sampling": run.sampling_artifact_id,
        },
        source_snapshot=_snapshot_view(session, run.snapshot_id),
        error=run.error,
        error_details=run.error_details,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
