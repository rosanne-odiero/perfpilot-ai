"""Profile and benchmark runs, found through their jobs (the API has no run list endpoint)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from frontend import data
from frontend.api_client import ApiError
from frontend.formatting import TERMINAL_JOB_STATES

RunKind = Literal["profile", "benchmark"]
JOB_TYPES: dict[RunKind, str] = {"profile": "workload.profile", "benchmark": "workload.benchmark"}


@dataclass(frozen=True)
class RunRef:
    kind: RunKind
    id: str
    job_id: str
    job_status: str
    created_at: str
    run: dict[str, Any]

    @property
    def workload_id(self) -> str:
        return str(self.run.get("workload_id", ""))


def _load(kind: RunKind, run_id: str, terminal: bool) -> dict[str, Any]:
    if kind == "profile":
        return data.finished_profile(run_id) if terminal else data.profile(run_id)
    return data.finished_benchmark(run_id) if terminal else data.benchmark(run_id)


def runs(kind: RunKind, workload_id: str | None = None, limit: int = 30) -> list[RunRef]:
    found: list[RunRef] = []
    for job in data.jobs(JOB_TYPES[kind], limit):
        if not job.get("subject_id"):
            continue
        try:
            run = _load(kind, job["subject_id"], job["status"] in TERMINAL_JOB_STATES)
        except ApiError:
            continue
        ref = RunRef(kind, job["subject_id"], job["id"], job["status"], job["created_at"], run)
        if workload_id is None or ref.workload_id == workload_id:
            found.append(ref)
    return found
