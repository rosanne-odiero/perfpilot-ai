"""Job progress: a pure import → audit state machine plus polling widgets.

``advance`` is pure apart from the injected ``fetch_job`` / ``start_audit`` callables, so the
pipeline can be tested with fakes; the widgets poll ``GET /api/jobs/{id}`` from a Streamlit
fragment and trigger a full rerun once a job reaches a terminal state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError
from frontend.components.notices import show_api_error
from frontend.formatting import TERMINAL_JOB_STATES, humanize
from frontend.theme import esc

POLL_SECONDS = 1.0

IMPORT_STAGES = (("acquire", "Fetch source"), ("inventory", "Inventory"), ("finalize", "Seal"))
AUDIT_STAGES = (
    ("INDEXING", "Index"),
    ("GRAPHING", "Graph"),
    ("STATIC_RULES", "Static rules"),
    ("PRIORITIZING", "Prioritize"),
    ("AI_ANALYSIS", "AI analysis"),
    ("COMPLETED", "Done"),
)


@dataclass(frozen=True)
class JobState:
    id: str
    job_type: str
    status: str
    stage: str | None
    progress: float
    message: str | None
    error: str | None
    error_details: dict[str, Any] | None
    result: dict[str, Any] | None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> JobState:
        return cls(
            id=raw["id"],
            job_type=raw.get("job_type", ""),
            status=raw["status"],
            stage=raw.get("stage"),
            progress=float(raw.get("progress") or 0.0),
            message=raw.get("message"),
            error=raw.get("error"),
            error_details=raw.get("error_details"),
            result=raw.get("result"),
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATES

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


class Phase(StrEnum):
    IMPORTING = "IMPORTING"
    AUDITING = "AUDITING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class AuditPipeline:
    phase: Phase
    snapshot_id: str
    mode: str = "STATIC"
    use_llm: bool = False
    llm_consent: bool = False
    import_job_id: str | None = None
    audit_job_id: str | None = None
    audit_id: str | None = None
    current_job: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    error_details: dict[str, Any] | None = None

    @property
    def finished(self) -> bool:
        return self.phase in {Phase.DONE, Phase.FAILED}

    def audit_request(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "mode": self.mode,
            "use_llm": self.use_llm,
            "llm_consent": self.llm_consent,
        }

    def to_state(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> AuditPipeline:
        return cls(**{**state, "phase": Phase(state["phase"])})


FetchJob = Callable[[str], dict[str, Any]]
StartAudit = Callable[[dict[str, Any]], dict[str, Any]]


def _failed_job(
    pipeline: AuditPipeline, job: JobState, raw: dict[str, Any], what: str
) -> AuditPipeline:
    return replace(
        pipeline,
        phase=Phase.FAILED,
        current_job=raw,
        error=job.error or f"{what} ended with status {job.status}",
        error_code=job.status,
        error_details=job.error_details,
    )


def advance(pipeline: AuditPipeline, fetch_job: FetchJob, start_audit: StartAudit) -> AuditPipeline:
    """One polling step. Import success starts the audit; any failure stops the pipeline."""
    if pipeline.finished:
        return pipeline
    if pipeline.phase is Phase.IMPORTING:
        if pipeline.import_job_id is not None:
            raw = fetch_job(pipeline.import_job_id)
            job = JobState.parse(raw)
            if not job.terminal:
                return replace(pipeline, current_job=raw)
            snapshot_status = (job.result or {}).get("status", "READY")
            if not job.succeeded or snapshot_status != "READY":
                return _failed_job(pipeline, job, raw, "import")
        try:
            accepted = start_audit(pipeline.audit_request())
        except ApiError as exc:
            return replace(
                pipeline,
                phase=Phase.FAILED,
                error=exc.message,
                error_code=exc.code,
                error_details=exc.details if isinstance(exc.details, dict) else None,
            )
        return replace(
            pipeline,
            phase=Phase.AUDITING,
            audit_job_id=accepted["job_id"],
            audit_id=accepted.get("subject_id"),
            current_job=None,
        )
    assert pipeline.audit_job_id is not None
    raw = fetch_job(pipeline.audit_job_id)
    job = JobState.parse(raw)
    if not job.terminal:
        return replace(pipeline, current_job=raw)
    if not job.succeeded:
        return _failed_job(pipeline, job, raw, "audit")
    return replace(pipeline, phase=Phase.DONE, current_job=raw)


def pipeline_steps(pipeline: AuditPipeline) -> list[tuple[str, str]]:
    """(label, state) per step; state is ``done``, ``active``, ``failed`` or ``todo``."""
    job = JobState.parse(pipeline.current_job) if pipeline.current_job else None
    stages = [("import", "Import")] + [
        (key, label) for key, label in AUDIT_STAGES if key != "AI_ANALYSIS" or pipeline.use_llm
    ]
    if pipeline.phase is Phase.DONE:
        return [(label, "done") for _, label in stages]
    if pipeline.phase is Phase.IMPORTING or (
        pipeline.phase is Phase.FAILED and pipeline.audit_job_id is None
    ):
        current = 0
    else:
        keys = [key for key, _ in stages]
        stage = job.stage if job and job.stage in keys else None
        current = keys.index(stage) if stage else 1
    failed = pipeline.phase is Phase.FAILED

    def state(index: int) -> str:
        if index < current:
            return "done"
        if index == current:
            return "failed" if failed else "active"
        return "todo"

    return [(label, state(i)) for i, (_, label) in enumerate(stages)]


def _stepper_html(steps: list[tuple[str, str]]) -> str:
    items = "".join(f'<span class="pp-step {state}">{esc(label)}</span>' for label, state in steps)
    return f'<div class="pp-stepper">{items}</div>'


def _job_line(job: JobState) -> str:
    stage = humanize(job.stage) if job.stage else job.status.lower()
    return f"{stage} — {job.message}" if job.message else stage


def _start_audit(body: dict[str, Any]) -> dict[str, Any]:
    return dict(api.post("/audits", json=body))


@st.fragment(run_every=POLL_SECONDS)
def _poll_pipeline(state_key: str) -> None:
    pipeline = AuditPipeline.from_state(st.session_state[state_key])
    try:
        updated = advance(pipeline, data.job, _start_audit)
    except ApiError as exc:
        show_api_error(exc, "Polling job status")
        return
    st.session_state[state_key] = updated.to_state()
    if updated.finished:
        st.cache_data.clear()
        st.rerun()
    st.markdown(_stepper_html(pipeline_steps(updated)), unsafe_allow_html=True)
    if updated.current_job:
        job = JobState.parse(updated.current_job)
        what = "Importing repository" if updated.phase is Phase.IMPORTING else "Auditing"
        st.progress(min(1.0, max(0.0, job.progress)), text=f"{what}: {_job_line(job)}")
    else:
        st.progress(0.0, text="Queued — waiting for the worker (`make worker`)")


def render_pipeline(state_key: str) -> AuditPipeline | None:
    """Draw the tracker for the pipeline stored under ``state_key``; return it when finished."""
    raw = st.session_state.get(state_key)
    if not raw:
        return None
    pipeline = AuditPipeline.from_state(raw)
    if pipeline.finished:
        st.markdown(_stepper_html(pipeline_steps(pipeline)), unsafe_allow_html=True)
        return pipeline
    with st.container(border=True):
        st.markdown("**Audit in progress**")
        _poll_pipeline(state_key)
    return None


def render_failure(pipeline: AuditPipeline) -> None:
    show_api_error(
        ApiError(
            0, pipeline.error_code or "failed", pipeline.error or "failed", pipeline.error_details
        ),
        "Import failed" if pipeline.audit_job_id is None else "Audit failed",
    )


@st.fragment(run_every=POLL_SECONDS)
def _poll_job(job_id: str, label: str) -> None:
    try:
        job = JobState.parse(data.job(job_id))
    except ApiError as exc:
        show_api_error(exc, f"{label} status")
        return
    if job.terminal:
        st.cache_data.clear()
        st.rerun()
    st.progress(min(1.0, max(0.0, job.progress)), text=f"{label}: {_job_line(job)}")


def track_job(job_id: str, label: str) -> JobState | None:
    """Return the job once terminal; while it runs, show a live progress bar and return None."""
    job = JobState.parse(data.job(job_id))
    if job.terminal:
        return job
    _poll_job(job_id, label)
    return None


def render_job_error(job: JobState, label: str) -> None:
    details = job.error_details if isinstance(job.error_details, dict) else None
    show_api_error(ApiError(0, job.status, job.error or job.status.lower(), details), label)
