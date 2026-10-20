from __future__ import annotations

from typing import Any

import pytest

from frontend.api_client import ApiError
from frontend.components.job_progress import (
    AuditPipeline,
    JobState,
    Phase,
    advance,
    pipeline_steps,
)


def job(job_id: str, status: str, stage: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "job_type": "x",
        "status": status,
        "stage": stage,
        "progress": 0.5,
        **extra,
    }


class FakeServer:
    """Scripted job states per id plus a recorder for audit starts."""

    def __init__(self, timeline: dict[str, list[dict[str, Any]]]) -> None:
        self.timeline = timeline
        self.started: list[dict[str, Any]] = []
        self.start_error: ApiError | None = None

    def fetch(self, job_id: str) -> dict[str, Any]:
        states = self.timeline[job_id]
        return states.pop(0) if len(states) > 1 else states[0]

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.start_error:
            raise self.start_error
        self.started.append(body)
        return {"job_id": "audit-job", "status": "QUEUED", "subject_id": "audit-1"}


def pipeline(**overrides: Any) -> AuditPipeline:
    return AuditPipeline(
        phase=Phase.IMPORTING, snapshot_id="snap-1", import_job_id="import-job", **overrides
    )


def run(p: AuditPipeline, server: FakeServer, steps: int) -> list[AuditPipeline]:
    seen = []
    for _ in range(steps):
        p = advance(p, server.fetch, server.start)
        seen.append(p)
    return seen


def test_import_then_audit_then_done() -> None:
    server = FakeServer(
        {
            "import-job": [
                job("import-job", "RUNNING", "inventory"),
                job("import-job", "SUCCEEDED", "finalize", result={"status": "READY"}),
            ],
            "audit-job": [
                job("audit-job", "RUNNING", "STATIC_RULES"),
                job("audit-job", "SUCCEEDED", "COMPLETED"),
            ],
        }
    )
    states = run(pipeline(mode="STATIC", use_llm=True, llm_consent=True), server, 4)
    assert [s.phase for s in states] == [
        Phase.IMPORTING,
        Phase.AUDITING,
        Phase.AUDITING,
        Phase.DONE,
    ]
    assert server.started == [
        {"snapshot_id": "snap-1", "mode": "STATIC", "use_llm": True, "llm_consent": True}
    ]
    assert states[-1].audit_id == "audit-1"
    assert states[-1].finished
    assert advance(states[-1], server.fetch, server.start) is states[-1]


def test_failed_import_stops_without_starting_an_audit() -> None:
    server = FakeServer(
        {
            "import-job": [
                job(
                    "import-job",
                    "FAILED",
                    "acquire",
                    error="path is not allowlisted",
                    error_details={"code": "security_violation"},
                )
            ]
        }
    )
    (state,) = run(pipeline(), server, 1)
    assert state.phase is Phase.FAILED
    assert state.error == "path is not allowlisted"
    assert state.error_details == {"code": "security_violation"}
    assert server.started == []


def test_import_job_success_but_snapshot_failed_is_a_failure() -> None:
    server = FakeServer(
        {"import-job": [job("import-job", "SUCCEEDED", result={"status": "FAILED"})]}
    )
    (state,) = run(pipeline(), server, 1)
    assert state.phase is Phase.FAILED
    assert server.started == []


def test_rejected_audit_start_surfaces_the_api_error() -> None:
    server = FakeServer(
        {"import-job": [job("import-job", "SUCCEEDED", result={"status": "READY"})]}
    )
    server.start_error = ApiError(409, "conflict", "snapshot not ready", {"snapshot": "snap-1"})
    (state,) = run(pipeline(), server, 1)
    assert (state.phase, state.error_code, state.error) == (
        Phase.FAILED,
        "conflict",
        "snapshot not ready",
    )
    assert state.error_details == {"snapshot": "snap-1"}


@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "CANCELLED"])
def test_audit_job_terminal_failures(status: str) -> None:
    server = FakeServer(
        {
            "import-job": [job("import-job", "SUCCEEDED", result={"status": "READY"})],
            "audit-job": [job("audit-job", status, "INDEXING", error=None)],
        }
    )
    states = run(pipeline(), server, 2)
    assert states[-1].phase is Phase.FAILED
    assert states[-1].error == f"audit ended with status {status}"


def test_existing_snapshot_starts_the_audit_immediately() -> None:
    server = FakeServer({"audit-job": [job("audit-job", "QUEUED")]})
    state = advance(
        AuditPipeline(phase=Phase.IMPORTING, snapshot_id="snap-9"), server.fetch, server.start
    )
    assert state.phase is Phase.AUDITING
    assert server.started[0]["snapshot_id"] == "snap-9"


def test_state_round_trip() -> None:
    original = pipeline(audit_job_id="a", current_job=job("a", "RUNNING"))
    assert AuditPipeline.from_state(original.to_state()) == original


def test_steps_follow_the_current_stage() -> None:
    importing = pipeline(current_job=job("import-job", "RUNNING", "inventory"))
    assert pipeline_steps(importing)[0] == ("Import", "active")
    assert all(state == "todo" for _, state in pipeline_steps(importing)[1:])
    auditing = AuditPipeline(
        phase=Phase.AUDITING,
        snapshot_id="s",
        audit_job_id="a",
        current_job=job("a", "RUNNING", "STATIC_RULES"),
    )
    steps = dict(pipeline_steps(auditing))
    assert steps["Import"] == steps["Index"] == steps["Graph"] == "done"
    assert steps["Static rules"] == "active"
    assert "AI analysis" not in steps
    with_ai = dict(pipeline_steps(AuditPipeline(phase=Phase.DONE, snapshot_id="s", use_llm=True)))
    assert with_ai["AI analysis"] == "done"
    failed = AuditPipeline(
        phase=Phase.FAILED,
        snapshot_id="s",
        audit_job_id="a",
        current_job=job("a", "FAILED", "GRAPHING"),
    )
    assert dict(pipeline_steps(failed))["Graph"] == "failed"


def test_job_state_parsing() -> None:
    parsed = JobState.parse(job("j", "TIMED_OUT", progress=None))
    assert parsed.terminal
    assert not parsed.succeeded
    assert parsed.progress == 0.0
