"""Allowlisted local directories, copied (never mounted) into the workspace."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from backend.core.config import Settings
from backend.core.exceptions import NotFoundError, SecurityViolationError, ValidationFailedError
from backend.ingestion.filters import SkipReason
from backend.ingestion.git_meta import read_git_metadata
from backend.ingestion.limits import IngestLimits, LimitTracker
from backend.ingestion.walk import skipped_file_info, walk_tree
from backend.models.audit_schemas import AcquiredSource, RepositoryRef, SourceFileInfo
from backend.models.enums import RepositorySourceType

_COPY_CHUNK = 1 << 20
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def resolve_allowed_local_path(raw: str, settings: Settings) -> Path:
    """Return the real path of ``raw`` if it is an existing directory inside the allowlist."""
    if not settings.local_allowlist:
        raise SecurityViolationError(
            "no local directories are allowlisted (set PERFPILOT_LOCAL_ALLOWLIST)"
        )
    if "\x00" in raw:
        raise SecurityViolationError("invalid local path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValidationFailedError("local_path must be an absolute path")
    if ".." in candidate.parts:
        raise SecurityViolationError("'..' is not allowed in local_path")
    real = Path(os.path.realpath(candidate))
    allowed = [Path(os.path.realpath(p.expanduser())) for p in settings.local_allowlist]
    if not any(real == root or real.is_relative_to(root) for root in allowed):
        raise SecurityViolationError("local_path is outside the configured allowlist")
    data_dir = settings.resolved_data_dir
    if real == data_dir or real.is_relative_to(data_dir):
        raise SecurityViolationError("local_path points into the PerfPilot data directory")
    if not real.exists():
        raise NotFoundError("local_path does not exist")
    if not real.is_dir():
        raise ValidationFailedError("local_path is not a directory")
    return real


def _copy_regular_file(source: Path, dest: Path, max_bytes: int) -> int | None:
    """Copy without following symlinks. Returns bytes copied, or None if the file exceeded
    ``max_bytes`` (it may have grown since it was listed) or is no longer a regular file."""
    src_fd = os.open(source, os.O_RDONLY | _NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(src_fd).st_mode):
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dst_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644)
        copied = 0
        try:
            while chunk := os.read(src_fd, _COPY_CHUNK):
                copied += len(chunk)
                if copied > max_bytes:
                    break
                os.write(dst_fd, chunk)
        finally:
            os.close(dst_fd)
        if copied > max_bytes:
            dest.unlink()
            return None
        return copied
    finally:
        os.close(src_fd)


class LocalDirectoryProvider:
    source_type = RepositorySourceType.LOCAL_DIRECTORY

    def acquire(
        self, ref: RepositoryRef, workspace_dir: Path, settings: Settings
    ) -> AcquiredSource:
        if not ref.local_path:
            raise ValidationFailedError("local_path is required")
        source = resolve_allowed_local_path(ref.local_path, settings)
        dest_root = workspace_dir.resolve()
        tracker = LimitTracker(IngestLimits.from_settings(settings))
        skipped: list[SourceFileInfo] = []
        for entry in walk_tree(source, tracker, excluded_dirs=[settings.resolved_data_dir]):
            if entry.skip_reason is not None:
                skipped.append(entry.skipped_info())
                continue
            dest = dest_root.joinpath(*entry.rel_path.split("/"))
            if not dest.resolve().is_relative_to(dest_root):
                raise SecurityViolationError("copy target escapes the workspace")
            try:
                copied = _copy_regular_file(entry.path, dest, settings.max_file_bytes)
            except OSError:
                skipped.append(skipped_file_info(entry.rel_path, entry.size, SkipReason.UNREADABLE))
                continue
            if copied is None:
                skipped.append(skipped_file_info(entry.rel_path, entry.size, SkipReason.OVERSIZED))
        return AcquiredSource(
            workspace_path=str(workspace_dir),
            source_type=self.source_type,
            display_name=ref.display_name or source.name,
            git=read_git_metadata(source, home=workspace_dir.parent),
            skipped=skipped,
            limits_hit=list(tracker.hit),
        )
