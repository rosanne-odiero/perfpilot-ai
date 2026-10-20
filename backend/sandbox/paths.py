"""Path containment helpers shared by the sandbox backend, discovery and approvals.

Every host path handed to Docker or read during discovery goes through these checks: it must be
absolute (or a safe relative path), must not itself be a symlink, and its fully resolved location
must stay inside an allowed root. Resolving first and comparing afterwards defeats ``..`` tricks
and symlinks that point outside the root.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from backend.core.exceptions import SecurityViolationError, ValidationFailedError

# Characters that would break Docker's CSV-style ``--mount`` syntax or a log line.
_FORBIDDEN_MOUNT_CHARS = frozenset(',"\n\r\0')


def safe_relative_path(value: str, *, what: str = "path") -> PurePosixPath:
    """Validate a POSIX relative path without ``..``, absolute roots, backslashes or NUL bytes."""
    if not value or "\0" in value or "\\" in value:
        raise SecurityViolationError(f"invalid {what}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("~"):
        raise SecurityViolationError(f"{what} must be relative: {value!r}")
    if any(part == ".." for part in path.parts):
        raise SecurityViolationError(f"{what} must not contain '..': {value!r}")
    return path


def resolve_contained(
    path: Path,
    roots: Iterable[Path],
    *,
    what: str,
    allow_root: bool = False,
) -> Path:
    """Return the resolved ``path`` if it exists strictly inside one of ``roots``.

    The path itself must not be a symlink (a symlinked mount source could be swapped later), and the
    resolved target must be inside a resolved root.
    """
    if not path.is_absolute():
        raise SecurityViolationError(f"{what} must be an absolute path")
    if "\0" in str(path):
        raise SecurityViolationError(f"{what} contains a NUL byte")
    if os.path.islink(path):
        raise SecurityViolationError(f"{what} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ValidationFailedError(f"{what} does not exist") from exc
    for root in roots:
        root_resolved = root.expanduser().resolve()
        if resolved.is_relative_to(root_resolved) and (allow_root or resolved != root_resolved):
            return resolved
    raise SecurityViolationError(f"{what} is outside the allowed directories")


def resolve_inside(base: Path, relative: str, *, what: str) -> Path:
    """Resolve ``relative`` against an already-resolved ``base`` and require it to stay inside."""
    rel = safe_relative_path(relative, what=what)
    candidate = (base / rel).resolve()
    if not candidate.is_relative_to(base):
        raise SecurityViolationError(f"{what} escapes the workspace: {relative!r}")
    return candidate


def check_mount_source(path: Path, *, what: str) -> None:
    if any(char in _FORBIDDEN_MOUNT_CHARS for char in str(path)):
        raise SecurityViolationError(f"{what} contains characters not allowed in a mount path")


def overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)
