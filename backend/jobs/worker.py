"""Persistent local job worker.

Run with ``python -m backend.jobs.worker``. Jobs are claimed from the database with a lease, and
each job executes in a fresh child process so the worker can enforce timeouts and cancellation
and survive crashes. If a worker dies, its expired leases are reclaimed by the next worker.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import socket
import time
import traceback
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from backend.core.config import Settings, get_settings
from backend.core.exceptions import PerfPilotError
from backend.core.logging import configure_logging, get_logger
from backend.core.observability import configure_tracing, log_context, span
from backend.core.redaction import redact_text
from backend.jobs.registry import JobCancelledError, JobContext, get_handler
from backend.models.database import init_engine, session_scope, utcnow
from backend.models.enums import JobStatus
from backend.models.orm.platform import Job

log = get_logger(__name__)


def _finish(job_id: str, status: JobStatus, **fields: Any) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        if JobStatus(job.status).is_terminal:
            return
        job.status = status
        job.finished_at = utcnow()
        job.lease_owner = None
        job.lease_expires_at = None
        for key, value in fields.items():
            setattr(job, key, value)


def job_log_fields(job: Job) -> dict[str, str]:
    """Identifiers to bind to every log record emitted while ``job`` runs."""
    fields = {"job_id": job.id, "job_type": job.job_type}
    for key, value in (job.payload or {}).items():
        if key.endswith("_id") and isinstance(value, str) and len(value) <= 64:
            fields[key] = value
    if job.subject_type and job.subject_id:
        fields.setdefault(f"{job.subject_type}_id", job.subject_id)
    return fields


def execute_job(job_id: str, settings: Settings | None = None) -> JobStatus:
    """Run a claimed job's handler in the current process and persist the outcome."""
    settings = settings or get_settings()
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise PerfPilotError(f"job {job_id} vanished")
        ctx = JobContext(
            job_id=job.id, job_type=job.job_type, payload=dict(job.payload), settings=settings
        )
        fields = job_log_fields(job)
    with log_context(**fields):
        return _run_handler(ctx)


def _run_handler(ctx: JobContext) -> JobStatus:
    job_id = ctx.job_id
    started = time.perf_counter()

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    try:
        with span(f"job.{ctx.job_type}"):
            handler = get_handler(ctx.job_type)
            result = handler(ctx)
    except JobCancelledError:
        ctx.close_stage("cancelled")
        _finish(job_id, JobStatus.CANCELLED, message="cancelled")
        log.info("job cancelled", extra={"status": "cancelled", "duration_ms": elapsed_ms()})
        return JobStatus.CANCELLED
    except PerfPilotError as exc:
        ctx.close_stage("failed")
        log.warning(
            "job failed",
            extra={"status": "failed", "error_code": exc.code, "duration_ms": elapsed_ms()},
        )
        _finish(
            job_id,
            JobStatus.FAILED,
            error=redact_text(exc.message).text,
            error_details={"code": exc.code, **exc.details},
        )
        return JobStatus.FAILED
    except Exception as exc:
        ctx.close_stage("failed")
        log.exception("job crashed", extra={"status": "failed", "duration_ms": elapsed_ms()})
        # Exception text can quote repository content (e.g. a SyntaxError line), and both fields
        # are returned by GET /api/jobs/{id}; redact them like any other exported text.
        _finish(
            job_id,
            JobStatus.FAILED,
            error=redact_text(f"{type(exc).__name__}: {exc}").text,
            error_details={
                "code": "unhandled_exception",
                "traceback": redact_text(traceback.format_exc()).text,
            },
        )
        return JobStatus.FAILED
    ctx.close_stage("ok")
    _finish(job_id, JobStatus.SUCCEEDED, result=result, progress=1.0, message="completed")
    log.info("job succeeded", extra={"status": "succeeded", "duration_ms": elapsed_ms()})
    return JobStatus.SUCCEEDED


def _child_main(job_id: str) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings.otel_enabled)
    init_engine(settings, prepare_schema=False)
    execute_job(job_id, settings)


class Worker:
    def __init__(self, settings: Settings | None = None, *, isolate: bool = True) -> None:
        self.settings = settings or get_settings()
        self.isolate = isolate
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self._stop = False
        self._mp = mp.get_context("spawn")

    # -- queue maintenance -------------------------------------------------------------
    def reclaim_expired_leases(self) -> int:
        now = utcnow()
        reclaimed = 0
        with session_scope() as session:
            stale = session.scalars(
                select(Job).where(Job.status == JobStatus.RUNNING, Job.lease_expires_at < now)
            ).all()
            for job in stale:
                reclaimed += 1
                if job.attempts < job.max_attempts:
                    job.status = JobStatus.QUEUED
                    job.message = "re-queued after worker lease expired"
                else:
                    job.status = JobStatus.FAILED
                    job.error = "worker lost while running job (lease expired)"
                    job.finished_at = now
                job.lease_owner = None
                job.lease_expires_at = None
        return reclaimed

    def claim_next(self) -> str | None:
        with session_scope() as session:
            candidate = session.scalar(
                select(Job.id)
                .where(Job.status == JobStatus.QUEUED)
                .order_by(Job.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            now = utcnow()
            claimed = session.execute(
                update(Job)
                .where(Job.id == candidate, Job.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING,
                    lease_owner=self.worker_id,
                    lease_expires_at=now + timedelta(seconds=self.settings.job_lease_seconds),
                    started_at=now,
                    attempts=Job.attempts + 1,
                )
            )
            return candidate if claimed.rowcount == 1 else None  # type: ignore[attr-defined]

    def _heartbeat(self, job_id: str) -> tuple[bool, bool]:
        """Extend the lease. Returns (cancel_requested, timed_out)."""
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return True, False
            now = utcnow()
            job.lease_expires_at = now + timedelta(seconds=self.settings.job_lease_seconds)
            started = job.started_at or now
            if started.tzinfo is None:
                started = started.replace(tzinfo=now.tzinfo)
            timed_out = (now - started).total_seconds() > job.timeout_seconds
            return job.cancel_requested, timed_out

    # -- execution -------------------------------------------------------------------
    def run_job(self, job_id: str) -> None:
        if not self.isolate:
            execute_job(job_id, self.settings)
            return
        proc = self._mp.Process(target=_child_main, args=(job_id,), daemon=False)
        proc.start()
        interval = max(1.0, self.settings.job_lease_seconds / 4)
        while proc.is_alive():
            proc.join(timeout=min(interval, 2.0))
            if not proc.is_alive():
                break
            cancel, timed_out = self._heartbeat(job_id)
            if cancel or timed_out:
                proc.terminate()
                proc.join(timeout=10)
                if proc.is_alive():
                    proc.kill()
                if timed_out:
                    _finish(job_id, JobStatus.TIMED_OUT, error="job exceeded its timeout")
                else:
                    _finish(job_id, JobStatus.CANCELLED, message="cancelled")
                log.warning(
                    "job process terminated",
                    extra={"job_id": job_id, "status": "timed_out" if timed_out else "cancelled"},
                )
                return
        with session_scope() as session:
            job = session.get(Job, job_id)
            still_running = job is not None and job.status == JobStatus.RUNNING
        if still_running:
            _finish(
                job_id,
                JobStatus.FAILED,
                error=f"job process exited unexpectedly (exit code {proc.exitcode})",
            )

    def run_once(self) -> bool:
        self.reclaim_expired_leases()
        job_id = self.claim_next()
        if job_id is None:
            return False
        log.info("job claimed", extra={"job_id": job_id, "worker": self.worker_id})
        self.run_job(job_id)
        return True

    def drain(self, max_jobs: int = 100) -> int:
        """Run queued jobs until the queue is empty (used by tests and the CLI)."""
        count = 0
        while count < max_jobs and self.run_once():
            count += 1
        return count

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        log.info("worker started", extra={"worker": self.worker_id})
        while not self._stop:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)
        log.info("worker stopped", extra={"worker": self.worker_id})

    def stop(self) -> None:
        self._stop = True


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_tracing(settings.otel_enabled)
    init_engine(settings)
    Worker(settings).run_forever()


if __name__ == "__main__":
    main()
