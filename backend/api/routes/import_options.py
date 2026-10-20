"""Which import sources the server accepts and the limits it enforces (read-only, for clients).

Allowlisted directories are reported only as a count: their paths describe the server's file
system and are not needed by clients, which submit a path and get a clear error when it is not
allowed.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.api.deps import AppSettings

router = APIRouter(tags=["repositories"])


class ImportLimits(BaseModel):
    max_upload_bytes: int
    max_repo_files: int
    max_repo_bytes: int
    max_file_bytes: int
    max_python_files: int
    clone_timeout_seconds: int


class ImportOptionsOut(BaseModel):
    github_enabled: bool
    github_allowed_hosts: list[str]
    local_directory_enabled: bool = Field(
        description="True when PERFPILOT_LOCAL_ALLOWLIST lists at least one directory"
    )
    local_allowlist_roots: int
    limits: ImportLimits


@router.get("/import/options", response_model=ImportOptionsOut)
def import_options(settings: AppSettings) -> ImportOptionsOut:
    return ImportOptionsOut(
        github_enabled=bool(settings.github_allowed_hosts),
        github_allowed_hosts=list(settings.github_allowed_hosts),
        local_directory_enabled=bool(settings.local_allowlist),
        local_allowlist_roots=len(settings.local_allowlist),
        limits=ImportLimits(
            max_upload_bytes=settings.max_upload_bytes,
            max_repo_files=settings.max_repo_files,
            max_repo_bytes=settings.max_repo_bytes,
            max_file_bytes=settings.max_file_bytes,
            max_python_files=settings.max_python_files,
            clone_timeout_seconds=settings.clone_timeout_seconds,
        ),
    )
