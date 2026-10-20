"""Optimization lifecycle (base spec §17): explicit transition table plus guards.

The same machine drives two records:

* an ``AnalysisRun``: ``CREATED -> PROFILING -> PROFILED -> ANALYZING -> RECOMMENDED``
  (``CREATED -> PROFILED`` when an existing, successful profile is reused);
* an ``OptimizationCandidate``: ``RECOMMENDED -> PATCH_PROPOSED -> AWAITING_APPROVAL ->
  VALIDATING -> VALIDATED -> BENCHMARKING -> COMPLETED``.

``REJECTED``, ``FAILED`` and ``COMPLETED`` are terminal. Guards turn the product rules into
code: benchmarking needs passed correctness checks, approval needs a valid diff, a rejected
candidate can never complete, a failed profile cannot yield hotspots, and a benchmark whose
input differs from the baseline cannot complete the lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from backend.core.exceptions import InvalidStateTransitionError
from backend.models.database import utcnow


class OptimizationState(StrEnum):
    CREATED = "CREATED"
    PROFILING = "PROFILING"
    PROFILED = "PROFILED"
    ANALYZING = "ANALYZING"
    RECOMMENDED = "RECOMMENDED"
    PATCH_PROPOSED = "PATCH_PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    BENCHMARKING = "BENCHMARKING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return not TRANSITIONS[self]


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ValidationStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_RUN = "NOT_RUN"


S = OptimizationState

TRANSITIONS: dict[OptimizationState, frozenset[OptimizationState]] = {
    S.CREATED: frozenset({S.PROFILING, S.PROFILED, S.FAILED}),
    S.PROFILING: frozenset({S.PROFILED, S.FAILED}),
    S.PROFILED: frozenset({S.ANALYZING, S.FAILED}),
    S.ANALYZING: frozenset({S.RECOMMENDED, S.FAILED}),
    S.RECOMMENDED: frozenset({S.PATCH_PROPOSED, S.FAILED}),
    S.PATCH_PROPOSED: frozenset({S.AWAITING_APPROVAL, S.REJECTED, S.FAILED}),
    # Approval itself is recorded on the candidate; the state only moves when validation starts.
    S.AWAITING_APPROVAL: frozenset({S.VALIDATING, S.REJECTED, S.FAILED}),
    # INCONCLUSIVE validation (e.g. a timeout) returns to the approved queue for a retry.
    S.VALIDATING: frozenset({S.VALIDATED, S.AWAITING_APPROVAL, S.FAILED}),
    S.VALIDATED: frozenset({S.BENCHMARKING, S.REJECTED, S.FAILED}),
    # A benchmark that crashed (not one that compared badly) may be retried from VALIDATED.
    S.BENCHMARKING: frozenset({S.COMPLETED, S.VALIDATED, S.FAILED}),
    S.COMPLETED: frozenset(),
    S.REJECTED: frozenset(),
    S.FAILED: frozenset(),
}


class TransitionFacts(BaseModel):
    """Evidence the guards inspect. Unknown facts default to the *unsafe* value."""

    profile_status: str | None = None
    hotspot_count: int = 0
    has_diff: bool = False
    diff_valid: bool = False
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    validation_status: ValidationStatus = ValidationStatus.NOT_RUN
    required_checks_passed: bool = False
    original_tree_unchanged: bool = False
    benchmark_status: str | None = None
    benchmark_input_matches_baseline: bool = False


class TransitionRecord(BaseModel):
    from_state: OptimizationState
    to_state: OptimizationState
    at: datetime = Field(default_factory=utcnow)
    reason: str = ""

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


Guard = Callable[[TransitionFacts], str | None]


def _profile_ok(facts: TransitionFacts) -> str | None:
    if facts.profile_status != "SUCCEEDED":
        return (
            f"a failed or unfinished profiling run (status {facts.profile_status}) cannot "
            "produce hotspots"
        )
    if facts.hotspot_count <= 0:
        return "the profiling run recorded no hotspots"
    return None


def _has_diff(facts: TransitionFacts) -> str | None:
    return None if facts.has_diff else "no patch diff was produced"


def _diff_valid(facts: TransitionFacts) -> str | None:
    if not facts.has_diff:
        return "no patch diff exists"
    return None if facts.diff_valid else "the patch diff did not pass the patch validator"


def _start_validation(facts: TransitionFacts) -> str | None:
    if problem := _diff_valid(facts):
        return problem
    if facts.approval_status != ApprovalStatus.APPROVED:
        return f"the candidate must be explicitly approved (approval is {facts.approval_status})"
    if not facts.original_tree_unchanged:
        return "the original source tree changed since the patch was generated"
    return None


def _checks_passed(facts: TransitionFacts) -> str | None:
    if facts.validation_status != ValidationStatus.PASSED or not facts.required_checks_passed:
        return f"required correctness checks have not passed (validation {facts.validation_status})"
    return None


def _validation_inconclusive(facts: TransitionFacts) -> str | None:
    if facts.validation_status != ValidationStatus.INCONCLUSIVE:
        return "only an INCONCLUSIVE validation may return to the approval queue"
    return None


def _start_benchmark(facts: TransitionFacts) -> str | None:
    if problem := _checks_passed(facts):
        return f"benchmarking cannot start: {problem}"
    if facts.approval_status != ApprovalStatus.APPROVED:
        return "benchmarking cannot start: the candidate is not approved"
    if not facts.original_tree_unchanged:
        return "benchmarking cannot start: the original source tree changed"
    return None


def _complete(facts: TransitionFacts) -> str | None:
    if facts.approval_status != ApprovalStatus.APPROVED:
        return "only an approved candidate can complete"
    if problem := _checks_passed(facts):
        return problem
    if facts.benchmark_status != "SUCCEEDED":
        return f"the benchmark did not succeed (status {facts.benchmark_status})"
    if not facts.benchmark_input_matches_baseline:
        return "the benchmark input differs from the baseline input; results are not comparable"
    return None


def _benchmark_crashed(facts: TransitionFacts) -> str | None:
    return None if facts.benchmark_status == "FAILED" else "the benchmark did not fail"


def _rejected(facts: TransitionFacts) -> str | None:
    if facts.approval_status != ApprovalStatus.REJECTED:
        return "a candidate is rejected only through an explicit rejection"
    return None


GUARDS: dict[tuple[OptimizationState, OptimizationState], Guard] = {
    (S.CREATED, S.PROFILED): _profile_ok,
    (S.PROFILING, S.PROFILED): _profile_ok,
    (S.PROFILED, S.ANALYZING): _profile_ok,
    (S.ANALYZING, S.RECOMMENDED): _profile_ok,
    (S.RECOMMENDED, S.PATCH_PROPOSED): _has_diff,
    (S.PATCH_PROPOSED, S.AWAITING_APPROVAL): _diff_valid,
    (S.AWAITING_APPROVAL, S.VALIDATING): _start_validation,
    (S.VALIDATING, S.VALIDATED): _checks_passed,
    (S.VALIDATING, S.AWAITING_APPROVAL): _validation_inconclusive,
    (S.VALIDATED, S.BENCHMARKING): _start_benchmark,
    (S.BENCHMARKING, S.COMPLETED): _complete,
    (S.BENCHMARKING, S.VALIDATED): _benchmark_crashed,
    (S.PATCH_PROPOSED, S.REJECTED): _rejected,
    (S.AWAITING_APPROVAL, S.REJECTED): _rejected,
    (S.VALIDATED, S.REJECTED): _rejected,
}


def is_allowed(current: OptimizationState, target: OptimizationState) -> bool:
    return target in TRANSITIONS[current]


def check_transition(
    current: OptimizationState, target: OptimizationState, facts: TransitionFacts
) -> str | None:
    """Return why ``current -> target`` is refused, or ``None`` when it is allowed."""
    if not is_allowed(current, target):
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "none (terminal state)"
        return f"transition {current} -> {target} is not allowed (allowed: {allowed})"
    guard = GUARDS.get((current, target))
    return guard(facts) if guard else None


def transition(
    current: OptimizationState,
    target: OptimizationState,
    facts: TransitionFacts | None = None,
    reason: str = "",
) -> TransitionRecord:
    """Validate ``current -> target`` and return the record to append to the history."""
    problem = check_transition(current, target, facts or TransitionFacts())
    if problem:
        raise InvalidStateTransitionError(
            problem, details={"from_state": current.value, "to_state": target.value}
        )
    return TransitionRecord(from_state=current, to_state=target, reason=reason)


def ensure_can_approve(state: OptimizationState, facts: TransitionFacts) -> None:
    """Approval is only possible while awaiting it, and only for a valid diff."""
    if state != S.AWAITING_APPROVAL:
        raise InvalidStateTransitionError(
            f"approval is only possible in AWAITING_APPROVAL (current state {state})",
            details={"state": state.value},
        )
    if problem := _diff_valid(facts):
        raise InvalidStateTransitionError(f"cannot approve: {problem}")
    if facts.approval_status != ApprovalStatus.PENDING:
        raise InvalidStateTransitionError(
            f"the candidate was already {facts.approval_status.lower()}"
        )
