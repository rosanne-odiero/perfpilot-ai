"""Applying state-machine transitions to persisted records and gathering guard facts."""

from __future__ import annotations

from pathlib import Path

from backend.models.orm.optimization import AnalysisRun, OptimizationCandidate, ValidationRun
from backend.optimization.state_machine import (
    ApprovalStatus,
    OptimizationState,
    TransitionFacts,
    TransitionRecord,
    ValidationStatus,
    transition,
)
from backend.services.source_snapshots import hash_tree


def advance(
    record: AnalysisRun | OptimizationCandidate,
    target: OptimizationState,
    facts: TransitionFacts | None = None,
    reason: str = "",
) -> TransitionRecord:
    """Validate the transition, then update ``state`` and append to ``state_history``."""
    entry = transition(OptimizationState(record.state), target, facts, reason)
    record.state = target.value
    record.state_history = [*record.state_history, entry.as_json()]
    return entry


def original_unchanged(candidate: OptimizationCandidate, workspace: Path) -> bool:
    return hash_tree(workspace)[0] == candidate.original_tree_sha256


def candidate_facts(
    candidate: OptimizationCandidate,
    *,
    workspace: Path | None = None,
    validation: ValidationRun | None = None,
    approval: ApprovalStatus | None = None,
    validation_status: ValidationStatus | None = None,
    benchmark_status: str | None = None,
    benchmark_input_matches_baseline: bool = False,
) -> TransitionFacts:
    status = validation_status or ValidationStatus(candidate.validation_status)
    run_passed = validation is None or validation.status == ValidationStatus.PASSED
    return TransitionFacts(
        has_diff=bool(candidate.proposed_diff),
        diff_valid=bool(candidate.patch_validation.get("valid")),
        approval_status=approval or ApprovalStatus(candidate.approval_status),
        validation_status=status,
        required_checks_passed=status == ValidationStatus.PASSED and run_passed,
        original_tree_unchanged=(
            original_unchanged(candidate, workspace) if workspace is not None else False
        ),
        benchmark_status=benchmark_status,
        benchmark_input_matches_baseline=benchmark_input_matches_baseline,
    )
