"""Domain exceptions mapped to HTTP responses by the API layer."""

from __future__ import annotations

from typing import Any


class PerfPilotError(Exception):
    """Base error. ``status_code`` and ``code`` drive the API error envelope."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(PerfPilotError):
    status_code = 404
    code = "not_found"


class ValidationFailedError(PerfPilotError):
    status_code = 422
    code = "validation_failed"


class ConflictError(PerfPilotError):
    status_code = 409
    code = "conflict"


class SecurityViolationError(PerfPilotError):
    """Raised when input attempts path traversal, disallowed hosts, unsafe archives, etc."""

    status_code = 400
    code = "security_violation"


class LimitExceededError(PerfPilotError):
    status_code = 413
    code = "limit_exceeded"


class NotConfiguredError(PerfPilotError):
    status_code = 501
    code = "not_configured"


class UnavailableError(PerfPilotError):
    """A required capability (e.g. sandbox backend) is unavailable in this environment."""

    status_code = 503
    code = "unavailable"


class InvalidStateTransitionError(ConflictError):
    code = "invalid_state_transition"


class ExternalServiceError(PerfPilotError):
    status_code = 502
    code = "external_service_error"
