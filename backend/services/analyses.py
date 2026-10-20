"""Workload analysis: profile (or reuse a profile) -> static rules -> ranked recommendations."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit.rule_engine import run_rules
from backend.core.config import Settings
from backend.core.exceptions import (
    ConflictError,
    NotFoundError,
    PerfPilotError,
    ValidationFailedError,
)
from backend.indexing.source import ParsedModule, parse_file
from backend.jobs.service import enqueue_job
from backend.models.audit_schemas import Finding
from backend.models.database import session_scope, utcnow
from backend.models.optimization_api import AnalysisOut, AnalysisRequest, RecommendationOut
from backend.models.orm.optimization import AnalysisRun, OptimizationRecommendation
from backend.models.orm.platform import Job
from backend.models.orm.workloads import Hotspot, ProfileRun, SourceSnapshot
from backend.models.workloads import ProfileRequest, RunStatus, WorkloadSpec
from backend.optimization.lifecycle import advance
from backend.optimization.patch_generator import PatchTarget, generate_patch
from backend.optimization.patch_validator import protected_category
from backend.optimization.recommendations import RecommendationDraft, build_recommendation
from backend.optimization.state_machine import OptimizationState, TransitionFacts
from backend.profiling.hotspots import HotspotRecord
from backend.services import profiling
from backend.services.source_snapshots import IGNORED_DIRS, create_snapshot
from backend.services.workloads import build_spec, get_workload, resolve_size

ProgressFn = Callable[[str, float, str | None], None]
ANALYSIS_JOB_TYPE = "optimization.analyze"
MAX_ANALYZED_FILES = 500


def _noop(_stage: str, _fraction: float, _message: str | None = None) -> None:
    return None


# ------------------------------------------------------------------------------------------
# Submission
# ------------------------------------------------------------------------------------------


def _checked_profile(session: Session, spec: WorkloadSpec, profile_id: str) -> ProfileRun:
    profile = session.get(ProfileRun, profile_id)
    if profile is None:
        raise NotFoundError(f"profile {profile_id} not found")
    if profile.workload_id != spec.id:
        raise ValidationFailedError("the profile belongs to a different workload")
    if profile.status != RunStatus.SUCCEEDED:
        raise ConflictError(
            f"profile {profile_id} is {profile.status}; a failed or unfinished profiling run "
            "cannot provide hotspots",
            details={"profile_status": profile.status},
        )
    return profile


def create_analysis(session: Session, request: AnalysisRequest) -> AnalysisRun:
    spec = build_spec(get_workload(session, request.workload_id))
    if request.profile_id:
        profile = _checked_profile(session, spec, request.profile_id)
        size: str | None = profile.size
    else:
        size = resolve_size(spec, request.size)
    run = AnalysisRun(
        workload_id=spec.id,
        profile_run_id=request.profile_id,
        profile_requested=request.profile_id is None,
        profile_size=size,
        state=OptimizationState.CREATED,
        state_history=[],
    )
    session.add(run)
    session.flush()
    return run


def submit_analysis(session: Session, request: AnalysisRequest) -> tuple[AnalysisRun, Job]:
    run = create_analysis(session, request)
    spec = build_spec(get_workload(session, run.workload_id))
    job = enqueue_job(
        session,
        ANALYSIS_JOB_TYPE,
        {"analysis_run_id": run.id},
        timeout_seconds=spec.timeout_seconds * 3 + 300,
        max_attempts=1,
        subject_type="analysis_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


# ------------------------------------------------------------------------------------------
# Execution
# ------------------------------------------------------------------------------------------


def _profile_facts(session: Session, profile_id: str) -> TransitionFacts:
    profile = session.get(ProfileRun, profile_id)
    count = len(
        session.scalars(select(Hotspot.id).where(Hotspot.profile_run_id == profile_id)).all()
    )
    return TransitionFacts(profile_status=profile.status if profile else None, hotspot_count=count)


def _python_sources(root: Path) -> list[str]:
    """Workload source files (never tests, fixtures, references or harness files)."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            if (
                name.endswith(".py")
                and not path.is_symlink()
                and protected_category(relative) is None
            ):
                found.append(relative)
    return found[:MAX_ANALYZED_FILES]


def _module_name(relative: str) -> str:
    parts = relative[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def static_findings(root: Path) -> tuple[list[Finding], list[str]]:
    modules: list[ParsedModule] = []
    warnings: list[str] = []
    for relative in _python_sources(root):
        module = parse_file(root, relative)
        module.module_name = _module_name(relative)
        if module.error:
            warnings.append(f"{relative}: {module.error}")
        modules.append(module)
    result = run_rules(modules)
    warnings.extend(f"{path}: {error}" for path, error in result.errors.items())
    return [item.finding for item in result.findings], warnings


def _profile_payload(profile: ProfileRun) -> dict[str, Any]:
    return {
        "id": profile.id,
        "status": profile.status,
        "size": profile.size,
        "input_sha256": profile.input_sha256,
        "total_profiled_time_ms": profile.total_profiled_time_ms,
        "wall_time_ms": profile.wall_time_ms,
    }


def _patch_strategy(root: Path, draft: RecommendationDraft) -> dict[str, Any]:
    """Dry-run the deterministic proposer so the UI can say up front whether a patch exists."""
    try:
        proposal = generate_patch(
            PatchTarget(
                source_root=root,
                target_file=draft.target_file,
                target_function=draft.target_function,
                line_start=draft.source_lines["start"],
                line_end=draft.source_lines["end"],
                rule_id=draft.rule_id,
                expected_sha256=draft.target_file_sha256,
            )
        )
    except PerfPilotError as exc:
        return {"deterministic_patch_available": False, "reason": exc.message}
    return {
        "deterministic_patch_available": proposal.status == "proposed",
        "transform": proposal.transform,
        "reason": proposal.reason,
    }


def _uncovered_hotspots(
    hotspots: list[HotspotRecord], drafts: list[RecommendationDraft]
) -> list[str]:
    covered = {
        (d.profiler_evidence["target_function"] or {}).get("function_name")
        for d in drafts
        if d.profiler_evidence.get("target_function")
    }
    return [
        f"{h.function_name} ({h.file_path}:{h.line_number}, {h.cumulative_percentage:.1f}% "
        "cumulative) has no deterministic rule match"
        for h in sorted(hotspots, key=lambda h: h.cumulative_time_ms, reverse=True)
        if h.is_user_code and h.function_name not in covered and h.cumulative_percentage >= 5
    ][:5]


def _analyze(session: Session, run: AnalysisRun, spec: WorkloadSpec) -> None:
    assert run.profile_run_id is not None
    profile = session.get(ProfileRun, run.profile_run_id)
    assert profile is not None
    root = Path(spec.workspace_path).resolve()
    snapshot = create_snapshot(
        session, workload_id=spec.id, role="analysis", label=f"analysis {run.id}", root=root
    )
    run.source_snapshot_id = snapshot.id
    findings, warnings = static_findings(root)
    rows = session.scalars(select(Hotspot).where(Hotspot.profile_run_id == profile.id)).all()
    hotspots = [HotspotRecord.model_validate(row, from_attributes=True) for row in rows]
    payload = _profile_payload(profile)
    drafts = [
        build_recommendation(
            finding,
            spec=spec,
            profile=payload,
            hotspots=hotspots,
            file_sha256=snapshot.files.get(finding.location.file_path),
        )
        for finding in findings
    ]
    drafts.sort(key=lambda d: (-d.measured_share, d.target_file, d.source_lines["start"]))
    for rank, draft in enumerate(drafts, start=1):
        session.add(
            OptimizationRecommendation(
                analysis_run_id=run.id,
                workload_id=spec.id,
                rank=rank,
                patch_strategy=_patch_strategy(root, draft),
                **draft.model_dump(exclude={"transform", "measured_share", "confidence"}),
                confidence=draft.confidence.value,
            )
        )
    run.findings = [finding.model_dump(mode="json") for finding in findings]
    run.warnings = warnings
    run.summary = {
        "profile_id": profile.id,
        "profile_size": profile.size,
        "files_analyzed": len(_python_sources(root)),
        "static_findings": len(findings),
        "recommendations": len(drafts),
        "hotspots_without_rule": _uncovered_hotspots(hotspots, drafts),
        "provenance": {"findings": "STATIC", "profiler_evidence": "MEASURED"},
    }


def execute_analysis(
    run_id: str, settings: Settings, progress: ProgressFn | None = None
) -> AnalysisRun:
    report = progress or _noop
    try:
        with session_scope() as session:
            run = _get(session, run_id)
            spec = build_spec(get_workload(session, run.workload_id))
            run.started_at = utcnow()
            profile_id = run.profile_run_id
            if profile_id is None:
                advance(run, OptimizationState.PROFILING, reason="profiling the workload")
                profile = profiling.create_profile_run(
                    session, ProfileRequest(workload_id=spec.id, size=run.profile_size)
                )
                run.profile_run_id = profile_id = profile.id
                profile_pending = True
            else:
                profile_pending = False
        if profile_pending:
            report("profiling", 0.05, "profiling the workload (cProfile + tracemalloc)")
            profiling.execute_profile_run(profile_id, settings)
        report("analyzing", 0.6, "running static rules and correlating hotspots")
        with session_scope() as session:
            run = _get(session, run_id)
            advance(
                run,
                OptimizationState.PROFILED,
                _profile_facts(session, profile_id),
                reason=f"profile {profile_id} succeeded",
            )
            advance(run, OptimizationState.ANALYZING, _profile_facts(session, profile_id))
            _analyze(session, run, spec)
            advance(
                run,
                OptimizationState.RECOMMENDED,
                _profile_facts(session, profile_id),
                reason=f"{run.summary['recommendations']} recommendation(s)",
            )
            run.finished_at = utcnow()
        with session_scope() as session:
            return _get(session, run_id)
    except PerfPilotError as exc:
        _fail(run_id, exc.message, {"code": exc.code, **exc.details})
        raise
    except Exception as exc:
        _fail(run_id, f"{type(exc).__name__}: {exc}", {"code": "internal_error"})
        raise


def _fail(run_id: str, message: str, details: dict[str, Any]) -> None:
    with session_scope() as session:
        run = session.get(AnalysisRun, run_id)
        if run is None or OptimizationState(run.state).is_terminal:
            return
        advance(run, OptimizationState.FAILED, reason=message)
        run.error = message
        run.error_details = details
        run.finished_at = utcnow()


def _get(session: Session, run_id: str) -> AnalysisRun:
    run = session.get(AnalysisRun, run_id)
    if run is None:
        raise NotFoundError(f"analysis {run_id} not found")
    return run


# ------------------------------------------------------------------------------------------
# Views
# ------------------------------------------------------------------------------------------


def list_recommendations(session: Session, run_id: str) -> list[RecommendationOut]:
    _get(session, run_id)
    rows = session.scalars(
        select(OptimizationRecommendation)
        .where(OptimizationRecommendation.analysis_run_id == run_id)
        .order_by(OptimizationRecommendation.rank)
    )
    return [RecommendationOut.model_validate(row) for row in rows]


def analysis_view(session: Session, run_id: str) -> AnalysisOut:
    run = _get(session, run_id)
    snapshot = (
        session.get(SourceSnapshot, run.source_snapshot_id) if run.source_snapshot_id else None
    )
    ids = session.scalars(
        select(OptimizationRecommendation.id)
        .where(OptimizationRecommendation.analysis_run_id == run_id)
        .order_by(OptimizationRecommendation.rank)
    ).all()
    return AnalysisOut(
        id=run.id,
        workload_id=run.workload_id,
        profile_run_id=run.profile_run_id,
        profile_requested=run.profile_requested,
        profile_size=run.profile_size,
        job_id=run.job_id,
        state=run.state,
        state_history=run.state_history,
        source_snapshot=(
            {"id": snapshot.id, "tree_sha256": snapshot.tree_sha256, "files": snapshot.files}
            if snapshot
            else None
        ),
        findings=run.findings,
        summary=run.summary,
        warnings=run.warnings,
        recommendation_ids=list(ids),
        error=run.error,
        error_details=run.error_details,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
