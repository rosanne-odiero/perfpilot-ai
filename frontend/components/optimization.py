"""Optimization deep dive helpers (pure): lifecycle stepper, allowed actions, check tables."""

from __future__ import annotations

from typing import Any

import pandas as pd

LIFECYCLE = ("Profile", "Analyze", "Recommend", "Patch", "Approve", "Validate", "Benchmark", "Done")
_ANALYSIS_STEP = {
    "CREATED": 0,
    "PROFILING": 0,
    "PROFILED": 1,
    "ANALYZING": 1,
    "RECOMMENDED": 3,
}
_CANDIDATE_STEP = {
    "RECOMMENDED": 3,
    "PATCH_PROPOSED": 3,
    "AWAITING_APPROVAL": 4,
    "VALIDATING": 5,
    "VALIDATED": 6,
    "BENCHMARKING": 6,
    "COMPLETED": 8,
}
VALIDATION_COLORS = {
    "PASSED": "#16a34a",
    "FAILED": "#dc2626",
    "INCONCLUSIVE": "#ca8a04",
    "NOT_RUN": "#64748b",
    "SKIPPED": "#64748b",
}


def approval_status(candidate: dict[str, Any]) -> str:
    return str((candidate.get("approval") or {}).get("status") or "PENDING")


REPO_LIFECYCLE = ("Finding", "Patch", "Approve", "Validate", "Benchmark", "Done")
_REPO_OFFSET = 2  # REPO_LIFECYCLE[i] == LIFECYCLE[i + 2] from "Patch" on


def _candidate_step(candidate: dict[str, Any]) -> tuple[int, bool]:
    """(index in ``LIFECYCLE`` of the current step, failed?)."""
    state = candidate["state"]
    if state in _CANDIDATE_STEP:
        step = _CANDIDATE_STEP[state]
        if state == "AWAITING_APPROVAL" and approval_status(candidate) == "APPROVED":
            step = 5
        return step, False
    if state == "REJECTED":
        by = (candidate.get("approval") or {}).get("rejected_by")
        return (3 if by == "patch-validator" else 4), True
    history = candidate.get("state_history") or []
    last = history[-1]["from_state"] if history else "RECOMMENDED"
    return _CANDIDATE_STEP.get(last, 3), True


def lifecycle_steps(
    analysis: dict[str, Any] | None, candidate: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """(label, state) per lifecycle step; state is ``done``, ``active``, ``failed`` or ``todo``."""
    failed = False
    if candidate is not None:
        current, failed = _candidate_step(candidate)
    elif analysis is not None:
        state = analysis["state"]
        if state == "FAILED":
            reached = {h.get("to_state") for h in analysis.get("state_history") or []}
            current, failed = (1 if "PROFILED" in reached else 0), True
        else:
            current = _ANALYSIS_STEP.get(state, 0)
    else:
        current = 0

    def state_of(index: int) -> str:
        if index < current:
            return "done"
        if index == current:
            return "failed" if failed else "active"
        return "todo"

    return [(label, state_of(i)) for i, label in enumerate(LIFECYCLE)]


def repo_lifecycle_steps(candidate: dict[str, Any]) -> list[tuple[str, str]]:
    """Stepper for a repository candidate: the finding replaces profile/analysis/recommendation."""
    current, failed = _candidate_step(candidate)
    index = max(1, current - _REPO_OFFSET)

    def state_of(i: int) -> str:
        if i < index:
            return "done"
        if i == index:
            return "failed" if failed else "active"
        return "todo"

    return [(label, state_of(i)) for i, label in enumerate(REPO_LIFECYCLE)]


def allowed_actions(candidate: dict[str, Any]) -> set[str]:
    """What the UI offers. Benchmarking requires a VALIDATED candidate whose validation PASSED."""
    state = candidate["state"]
    approval = approval_status(candidate)
    actions: set[str] = set()
    if state == "AWAITING_APPROVAL":
        actions.add("reject")
        actions.add("approve" if approval == "PENDING" else "validate")
    if state == "VALIDATED" and candidate.get("validation_status") == "PASSED":
        actions.add("benchmark")
    return actions


def benchmark_block_reason(candidate: dict[str, Any]) -> str | None:
    if "benchmark" in allowed_actions(candidate):
        return None
    state = candidate["state"]
    if state in {"BENCHMARKING", "COMPLETED"}:
        return f"The candidate is {state.lower()}; see its benchmark below."
    return (
        f"Only a VALIDATED candidate can be benchmarked (this one is {state}, validation "
        f"{candidate.get('validation_status', 'NOT_RUN')})."
    )


def checks_frame(checks: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Check": c.get("name", ""),
                "Required": bool(c.get("required")),
                "Status": c.get("status", ""),
                "Detail": c.get("detail", ""),
                "Seconds": c.get("duration_seconds"),
            }
            for c in checks
        ],
        columns=["Check", "Required", "Status", "Detail", "Seconds"],
    )


def history_frame(history: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "At": h.get("at", ""),
                "From": h.get("from_state", ""),
                "To": h.get("to_state", ""),
                "Reason": h.get("reason", ""),
            }
            for h in history
        ],
        columns=["At", "From", "To", "Reason"],
    )
