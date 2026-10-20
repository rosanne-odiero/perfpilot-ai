"""Optimization candidates for findings of an imported repository's audit (API side).

``create_candidate`` patches one finding of the audit's snapshot with the generic patch engine
(deterministic templates first; the LLM proposer only with audit-level consent and a configured
model), runs the strict patch validator, and materialises the patched file in an isolated
copy-on-write workspace. Approval is explicit. Validation and benchmarking are queued as worker
jobs that reuse a command the user approved for this audit; nothing here runs repository code or
talks to the sandbox (ADR-005).

Lifecycle (``backend/optimization/state_machine.py``)::

    RECOMMENDED -> PATCH_PROPOSED -> AWAITING_APPROVAL -(approve)-> VALIDATING -> VALIDATED
                -> BENCHMARKING -> COMPLETED          (REJECTED / FAILED are terminal)
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, NotFoundError, PerfPilotError, UnavailableError
from backend.jobs.service import enqueue_job
from backend.models.audit_schemas import Finding, RepositoryInventory
from backend.models.database import session_scope, utcnow
from backend.models.enums import AuditStatus, Provenance
from backend.models.optimization_api import DiffFileOut, DiffOut
from backend.models.orm.audit import AuditRun
from backend.models.orm.platform import Job
from backend.models.orm.repo_optimization import (
    RepoBenchmarkRun,
    RepoBenchmarkSample,
    RepoOptimizationCandidate,
    RepoValidationRun,
)
from backend.models.orm.runtime import RuntimeApproval, RuntimeApprovalStatus, RuntimeCommand
from backend.models.repo_optimization_api import (
    CandidateOutcome,
    RepoBenchmarkOut,
    RepoBenchmarkRequest,
    RepoBenchmarkSampleOut,
    RepoCandidateOut,
    RepoValidationOut,
)
from backend.optimization.patch_generator import (
    DETERMINISTIC,
    PatchProposal,
    PatchRequest,
    PatchTarget,
    select_patch_proposer,
)
from backend.optimization.patch_validator import PatchViolation, validate_patch
from backend.optimization.repo_workspace import (
    build_patched_copy,
    remove_candidate_workspace,
    verified_patched_copy,
)
from backend.optimization.state_machine import (
    ApprovalStatus,
    OptimizationState,
    TransitionFacts,
    ValidationStatus,
    ensure_can_approve,
    transition,
)
from backend.optimization.workspace import OriginalTreeChangedError, verify_original_unchanged
from backend.runtime.backends import latest_backend_status
from backend.sandbox.backend import SandboxLimits
from backend.sandbox.command_discovery import CandidateCommand
from backend.services import audits as audit_service
from backend.services.source_snapshots import hash_tree

VALIDATE_JOB = "repo_optimization.validate"
BENCHMARK_JOB = "repo_optimization.benchmark"
PATCH_VALIDATOR_ACTOR = "patch-validator"
TEST_COMMAND_KINDS = ("pytest",)
WORKLOAD_COMMAND_KINDS = ("script", "module", "pytest_benchmark")
JOB_MARGIN_SECONDS = 300
MAX_JOB_SECONDS = 24 * 3600
TIMED_DESCRIPTION = (
    "wall time (time.perf_counter_ns) and CPU time (time.process_time_ns) of the approved "
    "command only, measured inside the container by the harness without any profiler; container "
    "start-up, interpreter start-up and harness imports are excluded"
)

S = OptimizationState


# ----------------------------------------------------------------------------- helpers


def get_candidate(session: Session, candidate_id: str) -> RepoOptimizationCandidate:
    candidate = session.get(RepoOptimizationCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"repository optimization candidate {candidate_id} not found")
    return candidate


def advance(
    candidate: RepoOptimizationCandidate,
    target: OptimizationState,
    facts: TransitionFacts | None = None,
    reason: str = "",
) -> None:
    entry = transition(OptimizationState(candidate.state), target, facts, reason)
    candidate.state = target.value
    candidate.state_history = [*candidate.state_history, entry.as_json()]


def candidate_facts(
    candidate: RepoOptimizationCandidate,
    *,
    snapshot_root: Path | None = None,
    approval: ApprovalStatus | None = None,
    validation_status: ValidationStatus | None = None,
    benchmark_status: str | None = None,
    benchmark_input_matches_baseline: bool = False,
) -> TransitionFacts:
    status = validation_status or ValidationStatus(candidate.validation_status)
    return TransitionFacts(
        has_diff=bool(candidate.proposed_diff),
        diff_valid=bool(candidate.patch_validation.get("valid")),
        approval_status=approval or ApprovalStatus(candidate.approval_status),
        validation_status=status,
        required_checks_passed=status == ValidationStatus.PASSED,
        original_tree_unchanged=(
            hash_tree(snapshot_root)[0] == candidate.original_tree_sha256
            if snapshot_root is not None
            else False
        ),
        benchmark_status=benchmark_status,
        benchmark_input_matches_baseline=benchmark_input_matches_baseline,
    )


def snapshot_root(session: Session, settings: Settings, snapshot_id: str) -> Path:
    snapshot = audit_service.get_snapshot(session, snapshot_id)
    workspace_path, _ = audit_service.ensure_snapshot_ready(snapshot)
    return audit_service.resolve_workspace(settings, workspace_path)


def mark_failed(candidate_id: str, reason: str) -> None:
    """Persist a FAILED candidate in its own transaction (the caller's may be rolled back)."""
    with session_scope() as session:
        candidate = session.get(RepoOptimizationCandidate, candidate_id)
        if candidate is None or OptimizationState(candidate.state).is_terminal:
            return
        advance(candidate, S.FAILED, reason=reason)
        candidate.error = reason


def _require_snapshot_unchanged(
    candidate: RepoOptimizationCandidate, root: Path, when: str
) -> None:
    try:
        verify_original_unchanged(root, candidate.original_tree_sha256, when)
    except OriginalTreeChangedError as exc:
        mark_failed(candidate.id, exc.message)
        raise


def _commands(session: Session, audit_id: str) -> list[CandidateCommand]:
    rows = session.scalars(select(RuntimeCommand).where(RuntimeCommand.audit_id == audit_id))
    return [CandidateCommand.model_validate(row.command) for row in rows]


def _relative_to_workdir(workdir_rel: str, path: str) -> str:
    joined = path if workdir_rel == "." else f"{workdir_rel.rstrip('/')}/{path}"
    parts: list[str] = []
    for part in joined.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def measurement_inputs(session: Session, audit_id: str) -> list[str]:
    """Files the audit's discovered commands measure with or test against. A candidate may never
    modify them (benchmark scripts, test paths), in addition to the validator's own categories."""
    protected: set[str] = set()
    for command in _commands(session, audit_id):
        argv = command.argv
        if command.kind in ("script", "asv") and len(argv) == 2:
            protected.add(_relative_to_workdir(command.workdir_rel, argv[1]))
        elif command.kind in ("pytest", "pytest_benchmark") and argv[1:3] == ["-m", "pytest"]:
            args = iter(argv[3:])
            for arg in args:
                if arg == "-k":
                    next(args, None)
                elif not arg.startswith("-"):
                    path = arg.split("::", 1)[0]
                    protected.add(_relative_to_workdir(command.workdir_rel, path))
    return sorted(p for p in protected if p)


def _inventory_hash(inventory: dict[str, Any], target_file: str) -> str:
    files = RepositoryInventory.model_validate(inventory).files
    info = next((f for f in files if f.path == target_file), None)
    if info is None or info.skipped:
        raise ConflictError(f"{target_file} is not part of the analyzed snapshot")
    if not info.sha256:
        raise ConflictError(
            f"the snapshot recorded no content hash for {target_file}; re-import the repository"
        )
    return info.sha256


def _evidence(finding: Finding) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "rule_id": finding.rule_id,
        "severity": finding.severity.value,
        "confidence": finding.confidence.value,
        "description": finding.description,
        "suggestion": finding.suggestion,
        "provenance": finding.provenance.value,
    }
    if finding.measured is not None:
        evidence["measured (MEASURED, profiler)"] = finding.measured.model_dump(mode="json")
    return evidence


# ----------------------------------------------------------------------------- generation


def create_candidate(
    session: Session, settings: Settings, audit_id: str, finding_id: str
) -> RepoOptimizationCandidate:
    audit = audit_service.get_audit(session, audit_id)
    if audit.status not in (AuditStatus.SUCCEEDED, AuditStatus.PARTIAL):
        raise ConflictError(
            "candidates can only be generated for a finished audit",
            details={"status": audit.status},
        )
    finding = audit_service.get_finding(session, audit_id, finding_id)
    snapshot = audit_service.get_snapshot(session, audit.snapshot_id)
    _, inventory = audit_service.ensure_snapshot_ready(snapshot)
    root = snapshot_root(session, settings, audit.snapshot_id)
    location = finding.location
    expected_sha = _inventory_hash(inventory, location.file_path)
    original_tree = hash_tree(root)[0]
    protected = measurement_inputs(session, audit_id)
    proposer = select_patch_proposer(settings, allow_llm=audit.llm_consent)
    proposal = proposer.propose(
        PatchRequest(
            target=PatchTarget(
                source_root=root,
                target_file=location.file_path,
                target_function=location.symbol,
                line_start=location.line_start,
                line_end=location.line_end,
                rule_id=finding.rule_id,
                expected_sha256=expected_sha,
            ),
            title=finding.title,
            proposed_optimization=finding.suggestion,
            correctness_risks=[finding.tradeoffs] if finding.tradeoffs else [],
            evidence=_evidence(finding),
        )
    )
    candidate = _new_candidate(audit, finding, proposal, original_tree)
    session.add(candidate)
    session.flush()
    if proposal.status != "proposed" or not proposal.diff:
        candidate.error = proposal.reason or "the proposer returned no patch"
        advance(candidate, S.FAILED, reason=candidate.error)
        return candidate
    advance(
        candidate,
        S.PATCH_PROPOSED,
        candidate_facts(candidate),
        reason=f"diff proposed by {candidate.generated_by}",
    )
    if not _validate_diff(candidate, proposal, root, protected):
        return candidate
    advance(
        candidate,
        S.AWAITING_APPROVAL,
        candidate_facts(candidate),
        reason="diff passed the patch validator",
    )
    try:
        copy = build_patched_copy(
            settings,
            candidate_id=candidate.id,
            snapshot_root=root,
            original_tree_sha256=original_tree,
            diff=candidate.proposed_diff,
            target_file=candidate.target_file,
            patched_file_sha256=candidate.patched_file_sha256,
            extra_protected=protected,
        )
    except PerfPilotError as exc:
        candidate.error = f"the isolated workspace could not be prepared: {exc.message}"
        advance(candidate, S.FAILED, reason=candidate.error)
        return candidate
    candidate.workspace_path = str(copy.src)
    candidate.patched_tree_sha256 = copy.patched_tree_sha256
    candidate.notes = [
        *candidate.notes,
        f"Isolated workspace: {copy.hard_linked_files} file(s) hard-linked to the snapshot, "
        f"{copy.copied_files} copied; the patched file is a separate copy.",
    ]
    verify_original_unchanged(root, original_tree, "after patch generation")
    return candidate


def _new_candidate(
    audit: AuditRun, finding: Finding, proposal: PatchProposal, original_tree: str
) -> RepoOptimizationCandidate:
    generated_by = proposal.generated_by
    return RepoOptimizationCandidate(
        audit_id=audit.id,
        snapshot_id=audit.snapshot_id,
        finding_id=finding.id,
        rule_id=finding.rule_id,
        finding_title=finding.title[:255],
        evidence=Provenance.MEASURED if finding.measured is not None else Provenance.STATIC,
        measured_evidence=finding.measured.model_dump(mode="json") if finding.measured else None,
        state=S.RECOMMENDED,
        state_history=[],
        generated_by=generated_by,
        provenance=Provenance.STATIC if generated_by == DETERMINISTIC else Provenance.AI_HYPOTHESIS,
        transform=proposal.transform,
        target_file=finding.location.file_path,
        target_function=proposal.target_function or finding.location.symbol,
        line_start=finding.location.line_start,
        line_end=finding.location.line_end,
        original_file_sha256=proposal.original_sha256,
        original_tree_sha256=original_tree,
        proposed_diff=proposal.diff if proposal.status == "proposed" else "",
        patched_file_sha256=proposal.patched_sha256,
        original_source=proposal.original_source,
        patched_source=proposal.patched_source,
        summary=proposal.summary,
        notes=list(proposal.notes),
        proposer_attempts=list(proposal.attempts),
        llm_usage=proposal.llm_usage,
        approval_status=ApprovalStatus.PENDING,
        validation_status=ValidationStatus.NOT_RUN,
    )


def _validate_diff(
    candidate: RepoOptimizationCandidate,
    proposal: PatchProposal,
    root: Path,
    protected: Sequence[str],
) -> bool:
    validated = validate_patch(
        proposal.diff,
        workspace_root=root,
        allowed_files=[candidate.target_file],
        extra_protected=protected,
    )
    report = validated.report
    patched = validated.patched.get(candidate.target_file)
    if report.valid and proposal.patched_source and patched != proposal.patched_source:
        report.valid = False
        report.violations.append(
            PatchViolation(
                code="diff_mismatch",
                message="applying the diff does not reproduce the proposed file",
                path=candidate.target_file,
            )
        )
    candidate.patch_validation = report.model_dump(mode="json")
    if report.valid:
        if patched is not None:
            candidate.patched_source = patched
        return True
    candidate.approval_status = ApprovalStatus.REJECTED
    candidate.rejected_at = utcnow()
    candidate.rejected_by = PATCH_VALIDATOR_ACTOR
    candidate.rejection_reasons = [
        {"by": PATCH_VALIDATOR_ACTOR, "code": v.code, "reason": v.message, "path": v.path}
        for v in report.violations
    ]
    advance(
        candidate,
        S.REJECTED,
        candidate_facts(candidate),
        reason="the patch validator rejected the diff",
    )
    return False


def ensure_patched_copy(settings: Settings, candidate_id: str, root: Path) -> Path:
    """The candidate's patched copy; rebuilt from the snapshot and the validated diff when it is
    missing or no longer matches its recorded tree hash (a rebuild must reproduce that hash)."""
    with session_scope() as session:
        candidate = get_candidate(session, candidate_id)
        existing = verified_patched_copy(
            Path(candidate.workspace_path) if candidate.workspace_path else None,
            candidate.patched_tree_sha256,
        )
        if existing is not None:
            return existing
        copy = build_patched_copy(
            settings,
            candidate_id=candidate.id,
            snapshot_root=root,
            original_tree_sha256=candidate.original_tree_sha256,
            diff=candidate.proposed_diff,
            target_file=candidate.target_file,
            patched_file_sha256=candidate.patched_file_sha256,
            extra_protected=measurement_inputs(session, candidate.audit_id),
        )
        if candidate.patched_tree_sha256 not in (None, copy.patched_tree_sha256):
            raise ConflictError("the rebuilt patched workspace differs from the recorded one")
        candidate.workspace_path = str(copy.src)
        candidate.patched_tree_sha256 = copy.patched_tree_sha256
        return copy.src


# ----------------------------------------------------------------------------- approval


def approve_candidate(
    session: Session, settings: Settings, candidate_id: str, approved_by: str
) -> RepoOptimizationCandidate:
    candidate = get_candidate(session, candidate_id)
    ensure_can_approve(OptimizationState(candidate.state), candidate_facts(candidate))
    root = snapshot_root(session, settings, candidate.snapshot_id)
    _require_snapshot_unchanged(candidate, root, "before approval")
    now = utcnow()
    candidate.approval_status = ApprovalStatus.APPROVED
    candidate.approved_at = now
    candidate.approved_by = approved_by
    candidate.state_history = [
        *candidate.state_history,
        {
            "from_state": candidate.state,
            "to_state": candidate.state,
            "at": now.isoformat(),
            "reason": f"approved by {approved_by}",
        },
    ]
    return candidate


def reject_candidate(
    session: Session, settings: Settings, candidate_id: str, reason: str, rejected_by: str
) -> RepoOptimizationCandidate:
    candidate = get_candidate(session, candidate_id)
    advance(
        candidate,
        S.REJECTED,
        candidate_facts(candidate, approval=ApprovalStatus.REJECTED),
        reason=f"rejected by {rejected_by}: {reason}",
    )
    now = utcnow()
    candidate.approval_status = ApprovalStatus.REJECTED
    candidate.rejected_at = now
    candidate.rejected_by = rejected_by
    candidate.rejection_reasons = [
        *candidate.rejection_reasons,
        {"by": rejected_by, "reason": reason, "at": now.isoformat()},
    ]
    remove_candidate_workspace(settings, candidate.id)
    candidate.workspace_path = None
    return candidate


# ----------------------------------------------------------------------------- approved scopes


def approved_command(
    session: Session,
    candidate: RepoOptimizationCandidate,
    kinds: Sequence[str],
    approval_id: str | None,
    *,
    what: str,
) -> RuntimeApproval:
    """A command the user approved for this audit and snapshot (by design reused here: the user
    approved this exact argv, image and limits for sandbox execution of this snapshot).

    Consumed approvals (already used for a profile run) qualify; revoked ones never do, and
    unconsumed ones only until they expire."""
    stmt = (
        select(RuntimeApproval)
        .join(RuntimeCommand, RuntimeCommand.id == RuntimeApproval.command_id)
        .where(
            RuntimeApproval.audit_id == candidate.audit_id,
            RuntimeApproval.snapshot_id == candidate.snapshot_id,
            RuntimeApproval.status.in_(
                [RuntimeApprovalStatus.APPROVED, RuntimeApprovalStatus.CONSUMED]
            ),
            RuntimeCommand.kind.in_(list(kinds)),
        )
        .order_by(RuntimeApproval.approved_at.desc(), RuntimeApproval.created_at.desc())
    )
    if approval_id is not None:
        stmt = stmt.where(RuntimeApproval.id == approval_id)
    now = utcnow()
    for approval in session.scalars(stmt):
        expires = approval.expires_at
        expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
        if approval.status == RuntimeApprovalStatus.CONSUMED or expires > now:
            return approval
    raise ConflictError(
        f"no approved {what} command for this audit: approve one of the discovered "
        f"{'/'.join(kinds)} commands first (POST /api/audits/{candidate.audit_id}/runtime/"
        "approvals). PerfPilot never runs a command the user has not approved.",
        details={"required_kinds": list(kinds), "approval_id": approval_id},
    )


def _require_sandbox(session: Session, settings: Settings) -> None:
    status = latest_backend_status(session, settings)
    if not status.available:
        raise UnavailableError(
            f"runtime mode is disabled: {status.reason}",
            details={"backend": status.backend, "checked_at": str(status.checked_at)},
        )


def _job_timeout(limits: SandboxLimits, containers: int) -> int:
    return min(MAX_JOB_SECONDS, limits.timeout_seconds * containers + JOB_MARGIN_SECONDS)


def submit_validation(
    session: Session, settings: Settings, candidate_id: str, approval_id: str | None
) -> tuple[RepoValidationRun, Job]:
    candidate = get_candidate(session, candidate_id)
    root = snapshot_root(session, settings, candidate.snapshot_id)
    facts = candidate_facts(candidate, snapshot_root=root)
    if not facts.original_tree_unchanged:
        _require_snapshot_unchanged(candidate, root, "before validation")
    transition(OptimizationState(candidate.state), S.VALIDATING, facts)  # 409 if not allowed
    approval = approved_command(session, candidate, TEST_COMMAND_KINDS, approval_id, what="test")
    _require_sandbox(session, settings)
    advance(candidate, S.VALIDATING, facts, reason="validation requested")
    run = RepoValidationRun(
        candidate_id=candidate.id,
        audit_id=candidate.audit_id,
        approval_id=approval.id,
        status=ValidationStatus.NOT_RUN,
        argv=[str(a) for a in approval.argv],
        workdir_rel=approval.workdir_rel,
        image=approval.image,
        limits=dict(approval.limits),
        original_tree_sha256=candidate.original_tree_sha256,
    )
    session.add(run)
    session.flush()
    candidate.validation_run_id = run.id
    candidate.validation_status = ValidationStatus.NOT_RUN
    job = enqueue_job(
        session,
        VALIDATE_JOB,
        {"validation_run_id": run.id},
        idempotency_key=f"{VALIDATE_JOB}:{run.id}",
        timeout_seconds=_job_timeout(SandboxLimits.model_validate(approval.limits), 2),
        max_attempts=1,
        subject_type="repo_validation_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


def submit_benchmark(
    session: Session, settings: Settings, candidate_id: str, request: RepoBenchmarkRequest
) -> tuple[RepoBenchmarkRun, Job]:
    candidate = get_candidate(session, candidate_id)
    root = snapshot_root(session, settings, candidate.snapshot_id)
    facts = candidate_facts(candidate, snapshot_root=root)
    if not facts.original_tree_unchanged:
        _require_snapshot_unchanged(candidate, root, "before benchmarking")
    transition(OptimizationState(candidate.state), S.BENCHMARKING, facts)  # 409 if not allowed
    approval = approved_command(
        session, candidate, WORKLOAD_COMMAND_KINDS, request.approval_id, what="workload"
    )
    _require_sandbox(session, settings)
    advance(candidate, S.BENCHMARKING, facts, reason="benchmark requested")
    run = RepoBenchmarkRun(
        candidate_id=candidate.id,
        audit_id=candidate.audit_id,
        approval_id=approval.id,
        status="QUEUED",
        argv=[str(a) for a in approval.argv],
        workdir_rel=approval.workdir_rel,
        image=approval.image,
        limits=dict(approval.limits),
        warmups=request.warmups,
        runs=request.runs,
        threshold_percent=request.threshold_percent,
        seed=request.seed,
    )
    session.add(run)
    session.flush()
    candidate.benchmark_run_id = run.id
    containers = 2 * (request.warmups + request.runs)
    job = enqueue_job(
        session,
        BENCHMARK_JOB,
        {"benchmark_id": run.id},
        idempotency_key=f"{BENCHMARK_JOB}:{run.id}",
        timeout_seconds=_job_timeout(SandboxLimits.model_validate(approval.limits), containers),
        max_attempts=1,
        subject_type="repo_benchmark_run",
        subject_id=run.id,
    )
    run.job_id = job.id
    session.flush()
    return run, job


# ----------------------------------------------------------------------------- views


def _outcome(candidate: RepoOptimizationCandidate) -> CandidateOutcome:
    if not candidate.proposed_diff:
        return "no_safe_patch"
    if candidate.rejected_by == PATCH_VALIDATOR_ACTOR:
        return "rejected_by_validator"
    if candidate.approval_status == ApprovalStatus.REJECTED:
        return "rejected"
    return "proposed"


def validation_view(session: Session, validation_id: str) -> RepoValidationOut:
    run = session.get(RepoValidationRun, validation_id)
    if run is None:
        raise NotFoundError(f"validation {validation_id} not found")
    return RepoValidationOut.model_validate(run)


def benchmark_samples(session: Session, benchmark_id: str) -> list[RepoBenchmarkSample]:
    return list(
        session.scalars(
            select(RepoBenchmarkSample)
            .where(RepoBenchmarkSample.benchmark_id == benchmark_id)
            .order_by(RepoBenchmarkSample.sequence)
        )
    )


def benchmark_view(session: Session, benchmark_id: str) -> RepoBenchmarkOut:
    run = session.get(RepoBenchmarkRun, benchmark_id)
    if run is None:
        raise NotFoundError(f"benchmark {benchmark_id} not found")
    return RepoBenchmarkOut(
        id=run.id,
        candidate_id=run.candidate_id,
        audit_id=run.audit_id,
        approval_id=run.approval_id,
        job_id=run.job_id,
        status=run.status,
        argv=[str(a) for a in run.argv],
        workdir_rel=run.workdir_rel,
        image=run.image,
        limits=run.limits,
        warmups=run.warmups,
        runs=run.runs,
        threshold_percent=run.threshold_percent,
        seed=run.seed,
        timed=TIMED_DESCRIPTION,
        inputs=run.inputs,
        summary=run.summary,
        speedup=run.speedup,
        speedup_ci=[run.speedup_ci_low, run.speedup_ci_high],
        latency_reduction_percent=run.latency_reduction_percent,
        classification=run.classification,
        classification_reason=run.classification_reason,
        environment=run.environment,
        warnings=list(run.warnings or []),
        error=run.error,
        samples=[
            RepoBenchmarkSampleOut.model_validate(s) for s in benchmark_samples(session, run.id)
        ],
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def candidate_view(session: Session, candidate_id: str) -> RepoCandidateOut:
    candidate = get_candidate(session, candidate_id)
    return RepoCandidateOut(
        id=candidate.id,
        audit_id=candidate.audit_id,
        snapshot_id=candidate.snapshot_id,
        finding_id=candidate.finding_id,
        rule_id=candidate.rule_id,
        finding_title=candidate.finding_title,
        evidence="MEASURED" if candidate.evidence == Provenance.MEASURED else "STATIC",
        measured_evidence=candidate.measured_evidence,
        outcome=_outcome(candidate),
        state=candidate.state,
        state_history=candidate.state_history,
        generated_by=candidate.generated_by,
        provenance=candidate.provenance,
        transform=candidate.transform,
        target_file=candidate.target_file,
        target_function=candidate.target_function,
        line_start=candidate.line_start,
        line_end=candidate.line_end,
        original_file_sha256=candidate.original_file_sha256,
        original_tree_sha256=candidate.original_tree_sha256,
        patched_file_sha256=candidate.patched_file_sha256,
        patched_tree_sha256=candidate.patched_tree_sha256,
        summary=candidate.summary,
        notes=candidate.notes,
        proposer_attempts=candidate.proposer_attempts,
        llm_usage=candidate.llm_usage,
        patch_validation=candidate.patch_validation,
        approval={
            "status": candidate.approval_status,
            "approved_at": candidate.approved_at,
            "approved_by": candidate.approved_by,
            "rejected_at": candidate.rejected_at,
            "rejected_by": candidate.rejected_by,
        },
        rejection_reasons=candidate.rejection_reasons,
        workspace_path=candidate.workspace_path,
        validation_status=candidate.validation_status,
        validation=validation_view(session, candidate.validation_run_id)
        if candidate.validation_run_id
        else None,
        benchmark=benchmark_view(session, candidate.benchmark_run_id)
        if candidate.benchmark_run_id
        else None,
        error=candidate.error,
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def list_candidates(session: Session, audit_id: str) -> list[RepoCandidateOut]:
    audit_service.get_audit(session, audit_id)
    ids = session.scalars(
        select(RepoOptimizationCandidate.id)
        .where(RepoOptimizationCandidate.audit_id == audit_id)
        .order_by(RepoOptimizationCandidate.created_at, RepoOptimizationCandidate.id)
    )
    return [candidate_view(session, candidate_id) for candidate_id in ids]


def candidate_diff(session: Session, candidate_id: str) -> DiffOut:
    candidate = get_candidate(session, candidate_id)
    if not candidate.proposed_diff:
        raise NotFoundError(
            f"candidate {candidate_id} has no diff", details={"reason": candidate.error}
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
