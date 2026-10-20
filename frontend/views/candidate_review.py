"""Candidate review shared by workload and repository optimizations: diff, approve/reject,
validate, and the per-check table. The two APIs have the same lifecycle and action shapes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client as api
from frontend.api_client import ApiError
from frontend.components.diff_view import render_side_by_side, render_unified
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.notices import show_api_error
from frontend.components.optimization import (
    VALIDATION_COLORS,
    allowed_actions,
    approval_status,
)
from frontend.formatting import fmt_dt
from frontend.theme import badge, status_badge

ACTION_ERROR_KEY = "candidate_action_error"


@dataclass(frozen=True)
class CandidateApi:
    """``base`` is ``/optimizations`` (workloads) or ``/repo-optimizations`` (repositories)."""

    base: str
    load_diff: Callable[[str], dict[str, Any]]
    clear: Callable[[], None]
    validate_body: dict[str, Any] | None = None

    def job_key(self, candidate_id: str) -> str:
        return f"candidate_job_{candidate_id}"


def post_action(
    capi: CandidateApi,
    path: str,
    body: dict[str, Any] | None,
    candidate_id: str,
    job_action: str | None = None,
) -> None:
    """Widget callback for candidate actions; API errors are shown on the next run."""
    try:
        response = api.post(path, json=body) if body is not None else api.post(path)
    except ApiError as exc:
        st.session_state[ACTION_ERROR_KEY] = exc
        return
    if job_action and isinstance(response, dict) and response.get("job_id"):
        st.session_state[capi.job_key(candidate_id)] = (response["job_id"], job_action)
    capi.clear()


def show_action_error() -> None:
    error = st.session_state.pop(ACTION_ERROR_KEY, None)
    if isinstance(error, ApiError):
        show_api_error(error, "Request rejected")


def candidate_header(candidate: dict[str, Any], extra: str = "") -> None:
    transform = candidate.get("transform")
    st.markdown(
        f"#### Candidate `{candidate['id'][:8]}` {status_badge(candidate['state'])} "
        + (
            badge(f"approval {approval_status(candidate)}", "#334155") + " "
            if (candidate.get("patch_validation") or {}).get("valid")
            else ""
        )
        + badge(f"by {candidate.get('generated_by')}", "#1e293b")
        + (" " + badge(str(transform), "#312e81") if transform else "")
        + extra,
        unsafe_allow_html=True,
    )


def patch_validator_badges(candidate: dict[str, Any]) -> None:
    checks = (candidate.get("patch_validation") or {}).get("checks") or {}
    if checks:
        st.markdown(
            "Patch validator: "
            + " ".join(
                badge(name.replace("_", " "), "#16a34a" if result == "passed" else "#dc2626")
                for name, result in checks.items()
            ),
            unsafe_allow_html=True,
        )


def rejection_text(reason: Any) -> str:
    if isinstance(reason, dict):
        by = f" ({reason['by']})" if reason.get("by") else ""
        return f"{reason.get('reason') or reason.get('code') or reason}{by}"
    return str(reason)


def render_diff(capi: CandidateApi, candidate_id: str) -> None:
    try:
        diff = capi.load_diff(candidate_id)
    except ApiError as exc:
        st.caption(f"No diff to review: {exc.message}")
        return
    st.markdown(
        "##### Proposed diff " + badge(f"by {diff.get('generated_by', '?')}", "#1e293b"),
        unsafe_allow_html=True,
    )
    for file in diff.get("files") or []:
        render_side_by_side(
            file["original"],
            file["patched"],
            before_title=f"Original · {file['path']}",
            after_title=f"Candidate · {file['path']}",
        )
    with st.expander("Unified diff", expanded=False):
        render_unified(diff.get("unified_diff") or "", title="git-style unified diff")


def _reject_form(capi: CandidateApi, cid: str) -> None:
    reason = st.text_area("Rejection reason", key=f"reason_{cid}", height=80)
    st.button(
        "Reject candidate",
        icon="⛔",
        key=f"reject_{cid}",
        disabled=not reason.strip(),
        on_click=post_action,
        args=(capi, f"{capi.base}/{cid}/reject", {"reason": reason.strip()}, cid),
    )


def render_review(capi: CandidateApi, candidate: dict[str, Any], validate_note: str = "") -> None:
    cid = candidate["id"]
    actions = allowed_actions(candidate)
    if "approve" in actions:
        with st.container(border=True):
            st.markdown("##### Review")
            st.caption(
                "Approval is explicit and recorded. It only allows testing this diff on an "
                "isolated copy; the original source is never modified."
            )
            left, right = st.columns(2, gap="large")
            with left:
                who = st.text_input("Approved by", value="local-user", key=f"who_{cid}")
                reviewed = st.checkbox("I reviewed the diff above.", key=f"reviewed_{cid}")
                st.button(
                    "Approve candidate",
                    type="primary",
                    icon="✅",
                    key=f"approve_{cid}",
                    disabled=not reviewed or not who.strip(),
                    on_click=post_action,
                    args=(capi, f"{capi.base}/{cid}/approve", {"approved_by": who.strip()}, cid),
                )
            with right:
                _reject_form(capi, cid)
    elif "validate" in actions:
        approval = candidate.get("approval") or {}
        st.success(
            f"Approved by {approval.get('approved_by')} at {fmt_dt(approval.get('approved_at'))}.",
            icon="✅",
        )
        if validate_note:
            st.caption(validate_note)
        cols = st.columns([1, 2])
        cols[0].button(
            "Validate candidate",
            type="primary",
            icon="🧪",
            key=f"validate_{cid}",
            on_click=post_action,
            args=(capi, f"{capi.base}/{cid}/validate", capi.validate_body, cid, "validate"),
        )
        with cols[1], st.popover("Reject instead", icon="⛔"):
            _reject_form(capi, cid)


def status_style(value: object) -> str:
    color = VALIDATION_COLORS.get(str(value))
    return f"color:{color};font-weight:700" if color else ""


def render_checks(frame: pd.DataFrame, checks: list[dict[str, Any]]) -> None:
    if "Seconds" in frame and frame["Seconds"].isna().all():
        frame = frame.drop(columns="Seconds")
    st.dataframe(
        frame.style.map(status_style, subset=["Status"]),
        hide_index=True,
        width="stretch",
        column_config={"Detail": st.column_config.TextColumn(width="large")},
    )
    details = [c for c in checks if c.get("data")]
    if details:
        with st.expander("Check details"):
            for check in details:
                st.markdown(f"**{check.get('name')}** · {check.get('status')}")
                st.json(check["data"], expanded=False)


def track_candidate_job(
    capi: CandidateApi, candidate_id: str, running: tuple[str, str] | None
) -> bool:
    """Live tracker for a validation/benchmark job; True while it runs."""
    key = capi.job_key(candidate_id)
    pending = st.session_state.get(key) or running
    if not pending:
        return False
    job_id, action = pending
    label = "Validating" if action == "validate" else "Benchmarking"
    job = track_job(job_id, label)
    if job is None:
        return True
    st.session_state.pop(key, None)
    capi.clear()
    if not job.succeeded:
        render_job_error(job, label)
    return False
