"""Job handler registry.

Feature modules register handlers by placing a module in ``backend/jobs/handlers/`` that uses
``@job_handler("<job_type>")``. Modules are discovered automatically, so features never need to
edit a central list.
"""

from __future__ import annotations

import importlib
import pkgutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.core.config import Settings
from backend.core.exceptions import PerfPilotError
from backend.core.logging import get_logger
from backend.models.database import session_scope
from backend.models.enums import JobStatus
from backend.models.orm.platform import Job

log = get_logger(__name__)


class JobCancelledError(PerfPilotError):
    code = "job_cancelled"


@dataclass
class JobContext:
    job_id: str
    job_type: str
    payload: dict[str, Any]
    settings: Settings
    extra: dict[str, Any] = field(default_factory=dict)
    _stage: str | None = field(default=None, init=False, repr=False)
    _stage_started: float = field(default=0.0, init=False, repr=False)

    def progress(self, stage: str, fraction: float, message: str | None = None) -> None:
        """Persist progress so the API/UI can show it. Also acts as a cancellation point."""
        if stage != self._stage:
            self.close_stage("ok")
            self._stage, self._stage_started = stage, time.perf_counter()
        with session_scope() as session:
            job = session.get(Job, self.job_id)
            if job is None:
                return
            job.stage = stage
            job.progress = max(0.0, min(1.0, fraction))
            job.message = message
            cancel = job.cancel_requested
        if cancel:
            raise JobCancelledError("job cancelled by user")

    def close_stage(self, status: str) -> None:
        """Log the end of the current stage (stage boundaries carry ``duration_ms``)."""
        if self._stage is None:
            return
        duration_ms = round((time.perf_counter() - self._stage_started) * 1000, 1)
        log.info(
            "job stage finished",
            extra={"stage": self._stage, "status": status, "duration_ms": duration_ms},
        )
        self._stage = None

    def check_cancelled(self) -> None:
        with session_scope() as session:
            job = session.get(Job, self.job_id)
            if job is not None and (job.cancel_requested or job.status == JobStatus.CANCELLED):
                raise JobCancelledError("job cancelled by user")


JobHandler = Callable[[JobContext], dict[str, Any]]

_HANDLERS: dict[str, JobHandler] = {}
_discovered = False


def job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    def decorator(fn: JobHandler) -> JobHandler:
        if job_type in _HANDLERS and _HANDLERS[job_type] is not fn:
            raise RuntimeError(f"duplicate job handler for {job_type!r}")
        _HANDLERS[job_type] = fn
        return fn

    return decorator


def discover_handlers() -> None:
    global _discovered
    if _discovered:
        return
    import backend.jobs.handlers as handlers_pkg

    for info in pkgutil.iter_modules(handlers_pkg.__path__):
        importlib.import_module(f"{handlers_pkg.__name__}.{info.name}")
    _discovered = True


def get_handler(job_type: str) -> JobHandler:
    discover_handlers()
    try:
        return _HANDLERS[job_type]
    except KeyError as exc:
        raise PerfPilotError(f"no handler registered for job type {job_type!r}") from exc


def registered_job_types() -> list[str]:
    discover_handlers()
    return sorted(_HANDLERS)
