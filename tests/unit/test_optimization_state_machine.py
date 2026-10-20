from __future__ import annotations

import itertools

import pytest

from backend.core.exceptions import InvalidStateTransitionError
from backend.optimization.state_machine import (
    GUARDS,
    TRANSITIONS,
    ApprovalStatus,
    OptimizationState,
    TransitionFacts,
    ValidationStatus,
    check_transition,
    ensure_can_approve,
    transition,
)

S = OptimizationState

GOOD = TransitionFacts(
    profile_status="SUCCEEDED",
    hotspot_count=5,
    has_diff=True,
    diff_valid=True,
    approval_status=ApprovalStatus.APPROVED,
    validation_status=ValidationStatus.PASSED,
    required_checks_passed=True,
    original_tree_unchanged=True,
    benchmark_status="SUCCEEDED",
    benchmark_input_matches_baseline=True,
)
# Transitions whose guard needs facts that contradict GOOD.
SPECIAL = {
    (S.VALIDATING, S.AWAITING_APPROVAL): {"validation_status": ValidationStatus.INCONCLUSIVE},
    (S.BENCHMARKING, S.VALIDATED): {"benchmark_status": "FAILED"},
    (S.PATCH_PROPOSED, S.REJECTED): {"approval_status": ApprovalStatus.REJECTED},
    (S.AWAITING_APPROVAL, S.REJECTED): {"approval_status": ApprovalStatus.REJECTED},
    (S.VALIDATED, S.REJECTED): {"approval_status": ApprovalStatus.REJECTED},
}


def _facts(current: OptimizationState, target: OptimizationState) -> TransitionFacts:
    return GOOD.model_copy(update=SPECIAL.get((current, target), {}))


def test_every_state_has_a_row_and_terminal_states_are_final() -> None:
    assert set(TRANSITIONS) == set(OptimizationState)
    terminal = {s for s in OptimizationState if s.is_terminal}
    assert terminal == {S.COMPLETED, S.REJECTED, S.FAILED}
    for state in OptimizationState:
        if not state.is_terminal:
            assert S.FAILED in TRANSITIONS[state]


@pytest.mark.parametrize(
    ("current", "target"), list(itertools.product(OptimizationState, OptimizationState))
)
def test_transition_matrix(current: OptimizationState, target: OptimizationState) -> None:
    facts = _facts(current, target)
    if target in TRANSITIONS[current]:
        record = transition(current, target, facts, reason="test")
        assert (record.from_state, record.to_state) == (current, target)
        assert record.as_json()["reason"] == "test"
    else:
        with pytest.raises(InvalidStateTransitionError, match="not allowed"):
            transition(current, target, facts)


def test_happy_paths() -> None:
    analysis = [S.CREATED, S.PROFILING, S.PROFILED, S.ANALYZING, S.RECOMMENDED]
    candidate = [
        S.RECOMMENDED,
        S.PATCH_PROPOSED,
        S.AWAITING_APPROVAL,
        S.VALIDATING,
        S.VALIDATED,
        S.BENCHMARKING,
        S.COMPLETED,
    ]
    for path in (analysis, candidate):
        for current, target in itertools.pairwise(path):
            transition(current, target, GOOD)
    transition(S.CREATED, S.PROFILED, GOOD)  # reusing an existing successful profile


def test_benchmarking_requires_passed_correctness_checks() -> None:
    for status in (
        ValidationStatus.FAILED,
        ValidationStatus.INCONCLUSIVE,
        ValidationStatus.NOT_RUN,
    ):
        facts = GOOD.model_copy(update={"validation_status": status})
        problem = check_transition(S.VALIDATED, S.BENCHMARKING, facts)
        assert problem is not None
        assert "benchmarking cannot start" in problem
    facts = GOOD.model_copy(update={"required_checks_passed": False})
    assert check_transition(S.VALIDATED, S.BENCHMARKING, facts)
    # It is also impossible to jump to benchmarking from any earlier state.
    for state in (S.RECOMMENDED, S.PATCH_PROPOSED, S.AWAITING_APPROVAL, S.VALIDATING):
        assert check_transition(state, S.BENCHMARKING, GOOD)
    assert check_transition(S.VALIDATING, S.VALIDATED, facts)


def test_benchmarking_requires_unchanged_original_and_approval() -> None:
    changed = GOOD.model_copy(update={"original_tree_unchanged": False})
    assert "original source tree changed" in (
        check_transition(S.VALIDATED, S.BENCHMARKING, changed) or ""
    )
    pending = GOOD.model_copy(update={"approval_status": ApprovalStatus.PENDING})
    assert check_transition(S.VALIDATED, S.BENCHMARKING, pending)


def test_approval_impossible_before_a_valid_diff_exists() -> None:
    pending = GOOD.model_copy(update={"approval_status": ApprovalStatus.PENDING})
    ensure_can_approve(S.AWAITING_APPROVAL, pending)
    no_diff = pending.model_copy(update={"has_diff": False, "diff_valid": False})
    with pytest.raises(InvalidStateTransitionError, match="no patch diff exists"):
        ensure_can_approve(S.AWAITING_APPROVAL, no_diff)
    invalid = pending.model_copy(update={"diff_valid": False})
    with pytest.raises(InvalidStateTransitionError, match="patch validator"):
        ensure_can_approve(S.AWAITING_APPROVAL, invalid)
    for state in OptimizationState:
        if state != S.AWAITING_APPROVAL:
            with pytest.raises(InvalidStateTransitionError, match="only possible in"):
                ensure_can_approve(state, pending)
    with pytest.raises(InvalidStateTransitionError, match="already approved"):
        ensure_can_approve(S.AWAITING_APPROVAL, GOOD)
    # Validation cannot start without explicit approval or with an invalid diff.
    assert check_transition(S.AWAITING_APPROVAL, S.VALIDATING, pending)
    assert check_transition(
        S.PATCH_PROPOSED, S.AWAITING_APPROVAL, pending.model_copy(update={"diff_valid": False})
    )
    assert check_transition(S.RECOMMENDED, S.PATCH_PROPOSED, no_diff)


def test_rejected_candidates_can_never_become_successful() -> None:
    assert TRANSITIONS[S.REJECTED] == frozenset()
    for target in OptimizationState:
        assert check_transition(S.REJECTED, target, GOOD)
    rejected = GOOD.model_copy(update={"approval_status": ApprovalStatus.REJECTED})
    assert check_transition(S.BENCHMARKING, S.COMPLETED, rejected)
    assert check_transition(S.VALIDATED, S.BENCHMARKING, rejected)
    # A rejection is only ever recorded through an explicit rejection.
    assert check_transition(S.AWAITING_APPROVAL, S.REJECTED, GOOD)


def test_failed_profiling_run_cannot_produce_hotspots() -> None:
    for status in ("FAILED", "RUNNING", "PENDING", None):
        facts = GOOD.model_copy(update={"profile_status": status})
        problem = check_transition(S.PROFILING, S.PROFILED, facts)
        assert problem is not None
        assert "cannot produce hotspots" in problem
        assert check_transition(S.CREATED, S.PROFILED, facts)
        assert check_transition(S.ANALYZING, S.RECOMMENDED, facts)
    empty = GOOD.model_copy(update={"hotspot_count": 0})
    assert "no hotspots" in (check_transition(S.PROFILING, S.PROFILED, empty) or "")
    transition(S.PROFILING, S.FAILED, TransitionFacts(profile_status="FAILED"))


def test_benchmark_with_different_input_cannot_complete() -> None:
    different = GOOD.model_copy(update={"benchmark_input_matches_baseline": False})
    problem = check_transition(S.BENCHMARKING, S.COMPLETED, different)
    assert problem is not None
    assert "input differs" in problem
    failed = GOOD.model_copy(update={"benchmark_status": "FAILED"})
    assert check_transition(S.BENCHMARKING, S.COMPLETED, failed)
    transition(S.BENCHMARKING, S.FAILED, different)


def test_inconclusive_validation_returns_to_the_approval_queue_only() -> None:
    assert check_transition(S.VALIDATING, S.AWAITING_APPROVAL, GOOD)
    inconclusive = GOOD.model_copy(update={"validation_status": ValidationStatus.INCONCLUSIVE})
    transition(S.VALIDATING, S.AWAITING_APPROVAL, inconclusive)
    assert check_transition(S.VALIDATING, S.VALIDATED, inconclusive)


def test_guarded_transitions_refuse_default_facts() -> None:
    assert GUARDS
    for current, target in GUARDS:
        assert target in TRANSITIONS[current]
        assert check_transition(current, target, TransitionFacts()) is not None, (current, target)
