"""Typed ingestion errors. All map to the API error envelope via ``PerfPilotError``."""

from __future__ import annotations

from backend.core.exceptions import (
    ExternalServiceError,
    NotFoundError,
    SecurityViolationError,
)


class UnsafeArchiveError(SecurityViolationError):
    """The archive contains entries that could escape the workspace or exhaust resources."""

    code = "unsafe_archive"


class SensitiveFileError(SecurityViolationError):
    """The requested file is classified as sensitive; its content is never served."""

    status_code = 403
    code = "sensitive_file"


class RepositoryNotFoundError(NotFoundError):
    code = "repository_not_found"


class RefNotFoundError(NotFoundError):
    code = "ref_not_found"


class GitCommandError(ExternalServiceError):
    code = "git_error"


class CloneTimeoutError(ExternalServiceError):
    status_code = 504
    code = "clone_timeout"
