"""How the worker obtains its sandbox backend, and the availability it reports to the API.

ADR-005: only the worker talks to Docker. The worker records what it observed in
``runtime_backend_status``; the API reads that row and never builds a backend itself.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.exceptions import PerfPilotError
from backend.models.database import utcnow
from backend.models.orm.runtime_profiles import RuntimeBackendStatusRecord
from backend.sandbox.backend import BackendStatus, ExecutionBackend, get_execution_backend

BackendFactory = Callable[[Settings], ExecutionBackend]

# Status details that are safe to show to API clients. Host paths (e.g. the docker CLI location)
# and anything else the backend reports are dropped.
PUBLIC_DETAIL_KEYS = frozenset(
    {"server_version", "operating_system", "cgroup_version", "image", "image_id"}
)

_factory_override: BackendFactory | None = None


def set_backend_factory(factory: BackendFactory | None) -> None:
    """Test seam: replace how the *worker* builds its ExecutionBackend (``None`` restores it).

    This is not a product setting: there is no configuration that selects anything other than
    the Docker sandbox, and the API process never calls this or any backend.
    """
    global _factory_override
    _factory_override = factory


def worker_backend(settings: Settings) -> ExecutionBackend:
    """Build the worker's backend without checking availability (callers check and record)."""
    if _factory_override is not None:
        return _factory_override(settings)
    return get_execution_backend(settings, require_available=False)


def observe_backend(settings: Settings) -> tuple[ExecutionBackend | None, BackendStatus]:
    """Build the backend and check it. Configuration errors become an unavailable status."""
    try:
        backend = worker_backend(settings)
    except PerfPilotError as exc:
        return None, BackendStatus(
            available=False, backend=settings.sandbox_backend, reason=exc.message
        )
    return backend, backend.availability()


class PublicBackendStatus(BaseModel):
    backend: str
    available: bool
    reason: str | None
    details: dict[str, Any]
    checked_at: datetime | None


def public_details(details: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in details.items() if key in PUBLIC_DETAIL_KEYS}


def record_backend_status(
    session: Session, settings: Settings, status: BackendStatus
) -> RuntimeBackendStatusRecord:
    record = session.get(RuntimeBackendStatusRecord, settings.sandbox_backend)
    if record is None:
        record = RuntimeBackendStatusRecord(slot=settings.sandbox_backend)
        session.add(record)
    record.backend = status.backend
    record.available = status.available
    record.reason = status.reason
    record.details = public_details(status.details)
    record.checked_at = utcnow()
    session.flush()
    return record


def latest_backend_status(session: Session, settings: Settings) -> PublicBackendStatus:
    record = session.get(RuntimeBackendStatusRecord, settings.sandbox_backend)
    if record is None:
        return PublicBackendStatus(
            backend=settings.sandbox_backend,
            available=False,
            reason=(
                "no worker has checked the sandbox yet; run a RUNTIME audit or "
                "POST /api/runtime/status/refresh"
            ),
            details={},
            checked_at=None,
        )
    checked = record.checked_at
    return PublicBackendStatus(
        backend=record.backend,
        available=record.available,
        reason=record.reason,
        details=public_details(record.details or {}),
        checked_at=checked if checked.tzinfo else checked.replace(tzinfo=UTC),
    )
