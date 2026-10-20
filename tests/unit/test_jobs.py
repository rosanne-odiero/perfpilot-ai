from __future__ import annotations

import pytest

from backend.core.config import Settings
from backend.core.exceptions import ConflictError
from backend.jobs import service
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.enums import JobStatus
from backend.models.orm.platform import Job


def _enqueue(payload: dict[str, object], **kwargs: object) -> str:
    with session_scope() as session:
        return service.enqueue_job(session, "system.echo", payload, **kwargs).id  # type: ignore[arg-type]


def _job(job_id: str) -> Job:
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_inline_job_succeeds(settings: Settings) -> None:
    job_id = _enqueue({"message": "hi"})
    assert Worker(settings, isolate=False).drain() == 1
    job = _job(job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.result == {"echo": "hi"}
    assert job.attempts == 1


def test_inline_job_failure_is_recorded(settings: Settings) -> None:
    job_id = _enqueue({"fail": True})
    Worker(settings, isolate=False).drain()
    job = _job(job_id)
    assert job.status == JobStatus.FAILED
    assert "requested failure" in (job.error or "")


def test_idempotency_returns_same_job_and_rejects_conflicts(settings: Settings) -> None:
    first = _enqueue({"message": "a"}, idempotency_key="k1")
    second = _enqueue({"message": "a"}, idempotency_key="k1")
    assert first == second
    with pytest.raises(ConflictError):
        _enqueue({"message": "different"}, idempotency_key="k1")


def test_cancel_queued_job(settings: Settings) -> None:
    job_id = _enqueue({"message": "x"})
    with session_scope() as session:
        service.request_cancel(session, job_id)
    assert Worker(settings, isolate=False).drain() == 0
    assert _job(job_id).status == JobStatus.CANCELLED


def test_unknown_job_type_fails_cleanly(settings: Settings) -> None:
    with session_scope() as session:
        job_id = service.enqueue_job(session, "does.not.exist", {}).id
    Worker(settings, isolate=False).drain()
    job = _job(job_id)
    assert job.status == JobStatus.FAILED
    assert "no handler" in (job.error or "")


def test_expired_lease_is_requeued(settings: Settings) -> None:
    job_id = _enqueue({"message": "x"})
    worker = Worker(settings, isolate=False)
    assert worker.claim_next() == job_id
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        from datetime import timedelta

        from backend.models.database import utcnow

        job.lease_expires_at = utcnow() - timedelta(seconds=1)
    assert worker.reclaim_expired_leases() == 1
    assert _job(job_id).status == JobStatus.QUEUED


@pytest.mark.slow
def test_isolated_worker_enforces_timeout(settings: Settings) -> None:
    with session_scope() as session:
        job_id = service.enqueue_job(
            session, "system.echo", {"sleep_seconds": 20}, timeout_seconds=1
        ).id
    Worker(settings, isolate=True).drain()
    assert _job(job_id).status == JobStatus.TIMED_OUT


@pytest.mark.slow
def test_isolated_worker_runs_job_in_child_process(settings: Settings) -> None:
    job_id = _enqueue({"message": "child"})
    Worker(settings, isolate=True).drain()
    job = _job(job_id)
    assert job.status == JobStatus.SUCCEEDED, job.error
    assert job.result == {"echo": "child"}
