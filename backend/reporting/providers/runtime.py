"""Report provider for Runtime Audits: fills section 7 from real sandbox profile runs.

Only runs that SUCCEEDED contribute measurements. Failed, timed-out, OOM-killed or unavailable runs
are listed as limitations without numbers. Measured evidence on findings is read from the finding
rows themselves (attached by ``backend/audit/correlation.py``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.orm.runtime_profiles import RuntimeProfileRun, RuntimeRunStatus
from backend.reporting.model import RuntimeHotspot, RuntimeProfile
from backend.reporting.provider_registry import (
    ProviderContext,
    ProviderContribution,
    report_provider,
)
from backend.runtime.service import run_correlations, run_hotspots

REPORT_HOTSPOTS = 15
MIB = 1024 * 1024


def _environment(run: RuntimeProfileRun) -> dict[str, str]:
    env = run.environment or {}
    limits = env.get("limits") or run.limits or {}
    values = {
        "status": run.status,
        "approved_by": run.approved_by,
        "backend": env.get("backend"),
        "image": env.get("image"),
        "image_digest": env.get("image_digest"),
        "python": env.get("python_version"),
        "platform": env.get("platform"),
        "network": env.get("network"),
        "workdir": env.get("workdir"),
        "dependencies": env.get("dependencies"),
        "cpus": limits.get("cpus"),
        "memory_mb": limits.get("memory_mb"),
        "pids": limits.get("pids"),
        "timeout_seconds": limits.get("timeout_seconds"),
        "exit_code": run.exit_code,
        "duration_s (incl. container start/stop)": run.duration_s,
    }
    return {key: str(value) for key, value in values.items() if value is not None}


def _profile(session: Session, run: RuntimeProfileRun) -> RuntimeProfile:
    correlations, runtime_only = run_correlations(session, run.id)
    findings_by_hotspot: dict[str, list[str]] = {}
    for correlation in correlations:
        findings_by_hotspot.setdefault(correlation.hotspot_id, []).append(correlation.finding_id)
    hotspots = [
        RuntimeHotspot(
            function_name=row.symbol or row.function_name,
            file_path=row.file_path,
            line=row.line_number,
            internal_time_ms=row.internal_time_ms,
            cumulative_time_ms=row.cumulative_time_ms,
            call_count=row.total_calls,
            percentage_of_profiled_time=row.percentage_of_profiled_time,
            finding_ids=sorted(findings_by_hotspot.get(row.id, [])),
        )
        for row in run_hotspots(session, run.id, user_only=True, limit=REPORT_HOTSPOTS)
    ]
    profiler = "cProfile + tracemalloc (concurrent)" if run.trace_memory else "cProfile"
    notes = [
        f"Command run once in the isolated sandbox (network disabled) under {profiler}. Times "
        "include profiler overhead; compare functions by their share of profiled time. The "
        "table lists repository functions ordered by attributed time (self time plus direct "
        "library calls); 'Share' is the exclusive-time share.",
        "Peak RSS (resident memory): getrusage ru_maxrss of the profiled process inside the "
        "sandbox. It includes the interpreter, imported modules and the profiling harness.",
        "Peak traced memory (tracemalloc): peak of allocations made through Python's allocator "
        "during the command; memory allocated outside it (e.g. by C extensions) is not counted."
        if run.trace_memory
        else "Peak traced memory: not measured (tracemalloc was disabled for this run).",
    ]
    if correlations:
        notes.append(
            f"{len(correlations)} static finding(s) matched measured functions; their priority "
            "was re-ranked with measured_factor = 1 + 2 x attributed share (see section 5)."
        )
    if runtime_only:
        listed = "; ".join(
            f"{r.function_name} ({r.location.file_path}:{r.location.line_start}) "
            f"{r.attributed_percentage:.1f}%"
            for r in runtime_only[:10]
        )
        notes.append(f"Runtime-only hotspots (hot, but no static finding): {listed}.")
    return RuntimeProfile(
        profile_id=run.id,
        command=[str(a) for a in run.argv],
        profiler=profiler,
        wall_time_ms=run.wall_time_ms
        if run.wall_time_ms is not None
        else (run.duration_s or 0.0) * 1000.0,
        cpu_time_ms=run.cpu_time_ms,
        peak_rss_mb=run.peak_rss_bytes / MIB if run.peak_rss_bytes is not None else None,
        peak_traced_memory_mb=run.tracemalloc_peak_bytes / MIB
        if run.tracemalloc_peak_bytes is not None
        else None,
        hotspots=hotspots,
        environment=_environment(run),
        notes=notes,
    )


@report_provider("runtime-audit")
def runtime_audit(session: Session, ctx: ProviderContext) -> ProviderContribution | None:
    runs = list(
        session.scalars(
            select(RuntimeProfileRun)
            .where(RuntimeProfileRun.audit_id == ctx.audit_id)
            .order_by(RuntimeProfileRun.created_at, RuntimeProfileRun.id)
        )
    )
    if not runs:
        return None
    profiles = [_profile(session, r) for r in runs if r.status == RuntimeRunStatus.SUCCEEDED]
    limitations = [
        f"Runtime run {r.id} ({' '.join(str(a) for a in r.argv)}) produced no measurements "
        f"({r.status}): {r.failure_reason or 'no reason recorded'}."
        for r in runs
        if r.status
        in {
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.TIMED_OUT,
            RuntimeRunStatus.OOM_KILLED,
            RuntimeRunStatus.UNAVAILABLE,
        }
    ]
    if profiles:
        limitations.append(
            "Runtime measurements cover only the approved command(s); code paths they did not "
            "exercise keep their static priority and carry no measured evidence."
        )
    return ProviderContribution(runtime_profiles=profiles, limitations=limitations)
