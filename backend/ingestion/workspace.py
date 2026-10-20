"""Per-snapshot workspaces under ``<data_dir>/workspaces/<snapshot_id>/src``."""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from backend.core.config import Settings
from backend.core.exceptions import NotFoundError, SecurityViolationError, ValidationFailedError

_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


@dataclass(frozen=True)
class Workspace:
    snapshot_id: str
    root: Path
    src: Path


def _snapshot_root(settings: Settings, snapshot_id: str) -> Path:
    if not _SNAPSHOT_ID.match(snapshot_id):
        raise SecurityViolationError("invalid snapshot id for workspace")
    base = settings.workspaces_dir.resolve()
    root = (base / snapshot_id).resolve()
    if root.parent != base:
        raise SecurityViolationError("workspace path escapes the workspaces directory")
    return root


def create_workspace(settings: Settings, snapshot_id: str) -> Workspace:
    """Create an empty workspace, wiping leftovers from a previous (failed) attempt."""
    root = _snapshot_root(settings, snapshot_id)
    if root.exists():
        remove_tree(root)
    src = root / "src"
    src.mkdir(parents=True)
    return Workspace(snapshot_id=snapshot_id, root=root, src=src)


def get_workspace(settings: Settings, snapshot_id: str) -> Workspace:
    root = _snapshot_root(settings, snapshot_id)
    src = root / "src"
    if not src.is_dir():
        raise NotFoundError(f"workspace for snapshot {snapshot_id} not found")
    return Workspace(snapshot_id=snapshot_id, root=root, src=src)


def cleanup_workspace(settings: Settings, snapshot_id: str) -> bool:
    """Delete a snapshot workspace. Returns False when there was nothing to delete."""
    root = _snapshot_root(settings, snapshot_id)
    if not root.exists():
        return False
    remove_tree(root)
    return True


def normalize_rel_path(raw: str) -> str:
    """Validate a user-supplied repository-relative POSIX path and return its normal form."""
    if not raw or "\x00" in raw or "\\" in raw:
        raise ValidationFailedError("invalid path")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise SecurityViolationError("absolute paths are not allowed")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise ValidationFailedError("invalid path")
    if ".." in parts:
        raise SecurityViolationError("path traversal is not allowed")
    return str(PurePosixPath(*parts))


def resolve_in_workspace(src: Path, rel_path: str) -> Path:
    """Resolve a normalized relative path inside ``src`` without following symlinks out of it."""
    base = src.resolve()
    candidate = base.joinpath(*rel_path.split("/"))
    current = base
    for part in rel_path.split("/"):
        current = current / part
        if current.is_symlink():
            raise SecurityViolationError("symlinks inside the workspace are not served")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise SecurityViolationError("path escapes the workspace")
    return resolved


def make_read_only(root: Path) -> None:
    """Best effort: drop write permission on every file and directory under ``root``."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        for name in filenames:
            _chmod(Path(dirpath) / name, remove=stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        for name in dirnames:
            _chmod(Path(dirpath) / name, remove=stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    _chmod(root, remove=stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def _chmod(path: Path, *, remove: int = 0, add: int = 0) -> None:
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            return
        os.chmod(path, (stat.S_IMODE(st.st_mode) & ~remove) | add)
    except OSError:
        return


def remove_tree(root: Path) -> None:
    """Delete a workspace tree, restoring write permission first (it may be read-only)."""
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        for name in dirnames:
            _chmod(Path(dirpath) / name, add=stat.S_IRWXU)
    _chmod(root, add=stat.S_IRWXU)
    shutil.rmtree(root, ignore_errors=False)
