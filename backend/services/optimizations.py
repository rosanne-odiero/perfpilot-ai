"""Optimization candidates: generate -> validate the diff -> approve/reject -> validate behavior.

Every operation re-hashes the original workload tree and refuses to continue (marking the
candidate FAILED) if it changed. Approval is explicit and records who and when.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.exceptions import (
    ConflictError,
    NotConfiguredError,
    NotFoundError,
    PerfPilotError,
    ValidationFailedError,
)
from backend.infrastructure.artifact_store import ArtifactStore
from backend.jobs.service import enqueue_job
from backend.models.database import session_scope, utcnow
from backend.models.optimization_api import (
    CandidateOut,
    DiffFileOut,
    DiffOut,
    OptimizationRequest,
    ValidationRunOut,
)
from backend.models.orm.optimization import (
    AnalysisRun,
    OptimizationCandidate,
    OptimizationRecommendation,
    ValidationRun,
)
from backend.models.orm.platform import Job
from backend.models.orm.workloads import BenchmarkRun
from backend.models.workloads import WorkloadSpec
from backend.optimization.lifecycle import advance, candidate_facts
from backend.optimization.patch_generator import PatchRequest, PatchTarget, select_patch_proposer
from backend.optimization.patch_validator import PatchViolation, validate_patch
from backend.optimization.state_machine import (
    ApprovalStatus,
    OptimizationState,
    ValidationStatus,
    ensure_can_approve,
)
from backend.optimization.validation import ValidationInputs, run_validation
from backend.optimization.workspace import (
    OriginalTreeChangedError,
    prepare_workspace,
    verify_original_unchanged,
)
from backend.services.source_snapshots import hash_tree
from backend.services.workloads import build_spec, get_workload, resolve_target_file

ProgressFn = Callable[[str, float, str | None], None]
VALIDATE_JOB_TYPE = "optimization.validate"
PATCH_VALIDATOR_ACTOR = "patch-validator"


def workspace_of(spec: WorkloadSpec) -> Path:
    return Path(spec.workspace_path).resolve()


def protected_inputs(spec: WorkloadSpec) -> list[str]:
    """Workload-specific validation inputs that no candidate may modify."""
    root = workspace_of(spec)
    fixture = resolve_target_file(root, spec.fixture_id).relative_to(root).as_posix()
    return [fixture, *spec.validation.tests]


def get_candidate(session: Session, candidate_id: str) -> OptimizationCandidate:
    candidate = session.get(OptimizationCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"optimization candidate {candidate_id} not found")
    return candidate


def _spec(session: Session, candidate: OptimizationCandidate) -> WorkloadSpec:
    return build_spec(get_workload(session, candidate.workload_id))


def mark_failed(candidate_id: str, reason: str) -> None:
    """Persist a FAILED candidate in its own transaction (the caller's may be rolled back)."""
    with session_scope() as session:
        candidate = session.get(OptimizationCandidate, candidate_id)
        if candidate is None or OptimizationState(candidate.state).is_terminal:
            return
        advance(candidate, OptimizationState.FAILED, reason=reason)
        candidate.error = reason


def _require_original(candidate: OptimizationCandidate, spec: WorkloadSpec, when: str) -> None:
    try:
        verify_original_unchanged(workspace_of(spec), candidate.original_tree_sha256, when)
    except OriginalTreeChangedError as exc:
        mark_failed(candidate.id, exc.message)
        raise


# ------------------------------------------------------------------------------------------
# Candidate generation
# ------------------------------------------------------------------------------------------


def create_candidate(
    session: Session, settings: Settings, request: OptimizationRequest
) -> OptimizationCandidate:
    recommendation = session.get(OptimizationRecommendation, request.recommendation_id)
    if recommendation is None:
        raise NotFoundError(f"recommendation {request.recommendation_id} not found")
    analysis = session.get(AnalysisRun, recommendation.analysis_run_id)
    if analysis is None or analysis.state != OptimizationState.RECOMMENDED:
        raise ConflictError("the recommendation's analysis has not completed")
    if request.use_llm and not settings.llm_configured:
        raise NotConfiguredError(
            "use_llm requires a configured LLM endpoint (PERFPILOT_LLM_BASE_URL and _MODEL)"
        )
    spec = build_spec(get_workload(session, recommendation.workload_id))
    root = workspace_of(spec)
    original_tree = hash_tree(root)[0]
    proposer = select_patch_proposer(settings, allow_llm=request.use_llm)
    proposal = proposer.propose(
        PatchRequest(
            target=PatchTarget(
                source_root=root,
                target_file=recommendation.target_file,
                target_function=recommendation.target_function,
                line_start=int(recommendation.source_lines["start"]),
                line_end=int(recommendation.source_lines["end"]),
                rule_id=recommendation.rule_id or "",
                expected_sha256=recommendation.target_file_sha256,
            ),
            title=recommendation.title,
            proposed_optimization=recommendation.proposed_optimization,
            correctness_risks=[str(r) for r in recommendation.correctness_risks],
            evidence={
                "observed_bottleneck": recommendation.observed_bottleneck,
                "profiler_evidence (MEASURED)": recommendation.profiler_evidence,
            },
        )
    )
    candidate = OptimizationCandidate(
        recommendation_id=recommendation.id,
        workload_id=spec.id,
        state=OptimizationState.RECOMMENDED,
        state_history=[],
        generated_by=proposal.generated_by or proposer.name,
        transform=proposal.transform,
        target_file=recommendation.target_file,
        original_file_sha256=proposal.original_sha256,
        original_tree_sha256=original_tree,
        proposed_diff=proposal.diff if proposal.status == "proposed" else "",
        patched_file_sha256=proposal.patched_sha256,
        original_source=proposal.original_source,
        patched_source=proposal.patched_source,
        summary=proposal.summary,
        notes=[*proposal.notes, *(f"not proposed by {a}" for a in proposal.attempts)],
        approval_status=ApprovalStatus.PENDING,
        validation_status=ValidationStatus.NOT_RUN,
    )
    session.add(candidate)
    session.flush()
    if proposal.status != "proposed" or not proposal.diff:
        candidate.error = proposal.reason or "the proposer returned no patch"
        advance(candidate, OptimizationState.FAILED, reason=candidate.error)
        return candidate
    advance(
        candidate,
        OptimizationState.PATCH_PROPOSED,
        candidate_facts(candidate),
        reason=f"diff proposed by {candidate.generated_by}",
    )
    validated = validate_patch(
        proposal.diff,
        workspace_root=root,
        allowed_files=[recommendation.target_file],
        extra_protected=protected_inputs(spec),
    )
    report = validated.report
    patched = validated.patched.get(recommendation.target_file)
    if report.valid and proposal.patched_source and patched != proposal.patched_source:
        report.valid = False
        report.violations.append(
            PatchViolation(
                code="diff_mismatch",
                message="applying the diff does not reproduce the proposed file",
                path=recommendation.target_file,
            )
        )
    candidate.patch_validation = report.model_dump(mode="json")
    if not report.valid:
        candidate.approval_status = ApprovalStatus.REJECTED
        candidate.rejected_at = utcnow()
        candidate.rejected_by = PATCH_VALIDATOR_ACTOR
        candidate.rejection_reasons = [
            {"by": PATCH_VALIDATOR_ACTOR, "code": v.code, "reason": v.message, "path": v.path}
            for v in report.violations
        ]
        advance(
            candidate,
            OptimizationState.REJECTED,
            candidate_facts(candidate),
            reason="the patch validator rejected the diff",
        )
        return candidate
    if patched is not None:
        candidate.patched_source = patched
    advance(
        candidate,
        OptimizationState.AWAITING_APPROVAL,
        candidate_facts(candidate),
        reason="diff passed the patch validator",
    )
    verify_original_unchanged(root, original_tree, "after patch generation")
    return candidate


# ------------------------------------------------------------------------------------------
# Approval
# ------------------------------------------------------------------------------------------


def approve_candidate(
    session: Session, candidate_id: str, approved_by: str
) -> OptimizationCandidate:
    candidate = get_candidate(session, candidate_id)
    spec = _spec(session, candidate)
    ensure_can_approve(OptimizationState(candidate.state), candidate_facts(candidate))
    _require_original(candidate, spec, "before approval")
    candidate.approval_status = ApprovalStatus.APPROVED
    candidate.approved_at = utcnow()
    candidate.approved_by = approved_by
    candidate.state_history = [
        *candidate.state_history,
        {
            "from_state": candidate.state,
            "to_state": candidate.state,
            "at": utcnow().isoformat(),
            "reason": f"approved by {approved_by}",
        },
    ]
    return candidate


def reject_candidate(
    session: Session, candidate_id: str, reason: str, rejected_by: str
) -> OptimizationCandidate:
    candidate = get_candidate(session, candidate_id)
    advance(
        candidate,
        OptimizationState.REJECTED,
        candidate_facts(candidate, approval=ApprovalStatus.REJECTED),
        reason=f"rejected by {rejected_by}: {reason}",
    )
    candidate.approval_status = ApprovalStatus.REJECTED
    candidate.rejected_at = utcnow()
    candidate.rejected_by = rejected_by
    candidate.rejection_reasons = [
        *candidate.rejection_reasons,
        {"by": rejected_by, "reason": reason, "at": utcnow().isoformat()},
    ]
    return candidate


# ------------------------------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------------------------------


def create_validation(session: Session, candidate_id: str) -> ValidationRun:
    candidate = get_candidate(session, candidate_id)
    spec = _spec(session, candidate)
    root = workspace_of(spec)
    facts = candidate_facts(candidate, workspace=root)
    if not facts.original_tree_unchanged:
        _require_original(candidate, spec, "before validation")
    advance(candidate, OptimizationState.VALIDATING, facts, reason="validation requested")
    run = ValidationRun(
        candidate_id=candidate.id,
        workload_id=spec.id,
        status=ValidationStatus.NOT_RUN,
        original_tree_sha256=candidate.original_tree_sha256,
    )
    session.add(run)
    session.flush()
    candidate.validation_run_id = run.id
    candidate.validation_status = ValidationStatus.NOT_RUN
    return run


def submit_validation(session: Session, candidate_id: str) -> tuple[ValidationRun, Job]:
    run = create_validation(session, candidate_id)
    spec = build_spec(get_workload(session, run.workload_id))
    job = enqueue_job(
        session,
        VALIDATE_JOB_TYPE,
        {"validation_run_id": run.id},
        timeout_seconds=min(4 * 3600, spec.timeout_seconds * 40 + 300),
        max_attempts=1,
        subject_type="validation_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


def _finish_validation(
    validation_id: str,
    settings: Settings,
    status: ValidationStatus,
    *,
    checks: list[dict[str, Any]] | None = None,
    summary: str = "",
    hashes: dict[str, str] | None = None,
    log: str = "",
    error: str | None = None,
    patched_tree: str | None = None,
    duration: float | None = None,
) -> None:
    with session_scope() as session:
        run = session.get(ValidationRun, validation_id)
        assert run is not None
        candidate = get_candidate(session, run.candidate_id)
        run.status = status
        run.checks = checks or []
        run.summary = summary or (error or "")
        run.baseline_input_hashes = hashes or {}
        run.patched_tree_sha256 = patched_tree
        run.error = error
        run.duration_seconds = duration
        run.finished_at = utcnow()
        if log:
            artifact = ArtifactStore(settings).save_bytes(
                session,
                kind="validation",
                filename=f"validation-{validation_id}.log",
                data=log.encode("utf-8"),
                media_type="text/plain",
                owner_type="validation_run",
                owner_id=validation_id,
                meta={"format": "validation log (redacted)"},
            )
            run.logs_artifact_id = artifact.id
        candidate.validation_status = status
        if OptimizationState(candidate.state) != OptimizationState.VALIDATING:
            return
        facts = candidate_facts(candidate, validation=run)
        if status == ValidationStatus.PASSED:
            advance(candidate, OptimizationState.VALIDATED, facts, reason=summary)
        elif status == ValidationStatus.FAILED:
            candidate.error = summary or error
            advance(candidate, OptimizationState.FAILED, facts, reason=summary or (error or ""))
        else:
            facts.validation_status = ValidationStatus.INCONCLUSIVE
            advance(
                candidate,
                OptimizationState.AWAITING_APPROVAL,
                facts,
                reason=f"validation inconclusive: {summary or error}",
            )


def execute_validation(
    validation_id: str, settings: Settings, progress: ProgressFn | None = None
) -> ValidationRun:
    started = utcnow()
    with session_scope() as session:
        run = session.get(ValidationRun, validation_id)
        if run is None:
            raise NotFoundError(f"validation {validation_id} not found")
        candidate = get_candidate(session, run.candidate_id)
        spec = _spec(session, candidate)
        run.started_at = started
        candidate_id, diff = candidate.id, candidate.proposed_diff
        target_file, patched_sha = candidate.target_file, candidate.patched_file_sha256
        original_tree = candidate.original_tree_sha256
        recommendation = session.get(OptimizationRecommendation, candidate.recommendation_id)
        target_function = recommendation.target_function if recommendation else None
    root = workspace_of(spec)
    run_dir: Path | None = None
    try:
        if progress:
            progress("preparing", 0.05, "copying the workload and applying the approved patch")
        workspace = prepare_workspace(
            settings,
            candidate_id=candidate_id,
            slug=spec.slug,
            original_root=root,
            original_tree_sha256=original_tree,
            diff=diff,
            allowed_files=[target_file],
            patched_file_sha256=patched_sha,
            extra_protected=protected_inputs(spec),
        )
        with session_scope() as session:
            candidate = get_candidate(session, candidate_id)
            candidate.patched_workspace_path = str(workspace.patched_root)
            candidate.patched_tree_sha256 = workspace.patched_tree_sha256
        run_dir = workspace.root / "run"
        outcome = run_validation(
            ValidationInputs(
                spec=spec,
                baseline_root=workspace.baseline_root,
                patched_root=workspace.patched_root,
                target_file=target_file,
                target_function=target_function,
                run_dir=run_dir,
            ),
            progress,
        )
        verify_original_unchanged(root, original_tree, "after validation")
        if hash_tree(workspace.patched_root)[0] != workspace.patched_tree_sha256:
            raise ConflictError("the patched workspace changed while it was being validated")
    except (OriginalTreeChangedError, ValidationFailedError, ConflictError) as exc:
        _finish_validation(
            validation_id, settings, ValidationStatus.FAILED, error=exc.message, summary=exc.message
        )
        raise
    except PerfPilotError as exc:
        _finish_validation(
            validation_id, settings, ValidationStatus.INCONCLUSIVE, error=exc.message
        )
        raise
    except Exception as exc:
        _finish_validation(
            validation_id,
            settings,
            ValidationStatus.INCONCLUSIVE,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        if run_dir is not None:
            shutil.rmtree(run_dir, ignore_errors=True)
    _finish_validation(
        validation_id,
        settings,
        outcome.status,
        checks=[check.model_dump(mode="json") for check in outcome.checks],
        summary=outcome.summary,
        hashes=outcome.baseline_input_hashes,
        log=outcome.log,
        patched_tree=workspace.patched_tree_sha256,
        duration=(utcnow() - started).total_seconds(),
    )
    with session_scope() as session:
        result = session.get(ValidationRun, validation_id)
        assert result is not None
        return result


# ------------------------------------------------------------------------------------------
# Views
# ------------------------------------------------------------------------------------------


def _benchmark_summary(session: Session, candidate: OptimizationCandidate) -> dict[str, Any] | None:
    if candidate.benchmark_run_id is None:
        return None
    run = session.get(BenchmarkRun, candidate.benchmark_run_id)
    if run is None:
        return None
    summary = run.summary or {}
    return {
        "id": run.id,
        "status": run.status,
        "size": run.size,
        "classification": run.classification,
        "classification_reason": run.classification_reason,
        "speedup": run.speedup,
        "speedup_ci": [run.speedup_ci_low, run.speedup_ci_high],
        "latency_reduction_percent": run.latency_reduction_percent,
        "median_baseline_ms": ((summary.get("baseline") or {}).get("wall_ms") or {}).get("median"),
        "median_candidate_ms": ((summary.get("candidate") or {}).get("wall_ms") or {}).get(
            "median"
        ),
        "provenance": run.provenance,
    }


def validation_view(session: Session, validation_id: str) -> ValidationRunOut:
    run = session.get(ValidationRun, validation_id)
    if run is None:
        raise NotFoundError(f"validation {validation_id} not found")
    return ValidationRunOut.model_validate(run)


def candidate_view(session: Session, candidate_id: str) -> CandidateOut:
    candidate = get_candidate(session, candidate_id)
    validation = (
        validation_view(session, candidate.validation_run_id)
        if candidate.validation_run_id
        else None
    )
    return CandidateOut(
        id=candidate.id,
        recommendation_id=candidate.recommendation_id,
        workload_id=candidate.workload_id,
        state=candidate.state,
        state_history=candidate.state_history,
        generated_by=candidate.generated_by,
        transform=candidate.transform,
        target_file=candidate.target_file,
        original_file_sha256=candidate.original_file_sha256,
        original_tree_sha256=candidate.original_tree_sha256,
        patched_file_sha256=candidate.patched_file_sha256,
        patched_tree_sha256=candidate.patched_tree_sha256,
        summary=candidate.summary,
        notes=candidate.notes,
        patch_validation=candidate.patch_validation,
        approval={
            "status": candidate.approval_status,
            "approved_at": candidate.approved_at,
            "approved_by": candidate.approved_by,
            "rejected_at": candidate.rejected_at,
            "rejected_by": candidate.rejected_by,
        },
        rejection_reasons=candidate.rejection_reasons,
        patched_workspace_path=candidate.patched_workspace_path,
        validation_status=candidate.validation_status,
        validation_run_id=candidate.validation_run_id,
        validation=validation,
        benchmark_run_id=candidate.benchmark_run_id,
        benchmark=_benchmark_summary(session, candidate),
        error=candidate.error,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def candidate_diff(session: Session, candidate_id: str) -> DiffOut:
    candidate = get_candidate(session, candidate_id)
    if not candidate.proposed_diff:
        raise NotFoundError(
            f"candidate {candidate_id} has no diff",
            details={"reason": candidate.error},
        )
    return DiffOut(
        candidate_id=candidate.id,
        generated_by=candidate.generated_by,
        unified_diff=candidate.proposed_diff,
        files=[
            DiffFileOut(
                path=candidate.target_file,
                original=candidate.original_source,
                patched=candidate.patched_source,
                original_sha256=candidate.original_file_sha256,
                patched_sha256=candidate.patched_file_sha256,
            )
        ],
    )
