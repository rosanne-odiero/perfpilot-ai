"""Isolated, copy-on-write workspaces for repository optimization candidates.

``<data_dir>/workspaces/repo-optimizations/<candidate_id>/src`` mirrors the audit's snapshot. Files
are hard-linked to the snapshot where the filesystem allows it (falling back to a copy), and the
patched file is written to a *new* inode (temporary file + ``os.replace``), so the patch never
reaches the snapshot. The snapshot is only read: its tree hash is verified before and after every
step, and the copy must differ from it in exactly the target file. Like the snapshot, the copy is
left read-only. Nothing here executes code; the sandbox mounts the copy read-only.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, SecurityViolationError, ValidationFailedError
from backend.infrastructure.artifact_store import sha256_bytes
from backend.ingestion.workspace import remove_tree
from backend.optimization.patch_validator import validate_patch
from backend.optimization.workspace import changed_files, verify_original_unchanged
from backend.services.source_snapshots import IGNORED_DIRS, IGNORED_SUFFIXES, hash_tree

_CANDIDATE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WORKSPACES_SUBDIR = "repo-optimizations"


class PatchedCopy(BaseModel):
    root: Path
    src: Path
    patched_tree_sha256: str
    hard_linked_files: int
    copied_files: int


def candidate_root(settings: Settings, candidate_id: str) -> Path:
    if not _CANDIDATE_ID.match(candidate_id):
        raise SecurityViolationError("invalid candidate id")
    base = (settings.workspaces_dir / WORKSPACES_SUBDIR).resolve()
    path = (base / candidate_id).resolve()
    if not path.is_relative_to(base):
        raise SecurityViolationError("candidate workspace escapes the workspaces directory")
    return path


def _clone_tree(source: Path, dest: Path) -> tuple[int, int]:
    counts = {"linked": 0, "copied": 0}

    def link_or_copy(src: str, dst: str) -> str:
        try:
            os.link(src, dst, follow_symlinks=False)
            counts["linked"] += 1
        except OSError:
            shutil.copy2(src, dst, follow_symlinks=False)
            counts["copied"] += 1
        return dst

    shutil.copytree(
        source,
        dest,
        symlinks=True,
        copy_function=link_or_copy,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS, *(f"*{s}" for s in IGNORED_SUFFIXES)),
    )
    return counts["linked"], counts["copied"]


_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _directories(root: Path) -> list[Path]:
    found = [root]
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        found += [Path(dirpath) / name for name in dirnames]
    return [d for d in found if not d.is_symlink()]


def _set_directories_writable(root: Path, writable: bool) -> None:
    """Only directories of the copy are touched: files may be hard links shared with the snapshot,
    so changing their mode would change the snapshot too."""
    for directory in _directories(root):
        mode = stat.S_IMODE(directory.lstat().st_mode)
        os.chmod(directory, (mode | stat.S_IRWXU) if writable else (mode & ~_WRITE_BITS))


def _write_new_inode(path: Path, data: bytes) -> None:
    """Replace ``path`` with a new, read-only file; a hard link to the snapshot is broken, never
    written through."""
    mode = stat.S_IMODE(path.lstat().st_mode) & ~_WRITE_BITS
    tmp = path.with_name(f".{path.name}.perfpilot-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        remove_tree(path)


def build_patched_copy(
    settings: Settings,
    *,
    candidate_id: str,
    snapshot_root: Path,
    original_tree_sha256: str,
    diff: str,
    target_file: str,
    patched_file_sha256: str | None,
    extra_protected: Sequence[str] = (),
) -> PatchedCopy:
    snapshot_root = snapshot_root.resolve()
    verify_original_unchanged(snapshot_root, original_tree_sha256, "before copying")
    root = candidate_root(settings, candidate_id)
    src = root / "src"
    _remove(src)
    root.mkdir(parents=True, exist_ok=True)
    linked, copied = _clone_tree(snapshot_root, src)
    _set_directories_writable(src, True)
    if hash_tree(src)[0] != original_tree_sha256:
        raise ConflictError("the workspace copy does not match the snapshot")
    validated = validate_patch(
        diff, workspace_root=src, allowed_files=[target_file], extra_protected=extra_protected
    )
    if not validated.report.valid:
        raise ValidationFailedError(
            "the patch no longer passes the patch validator",
            details={"violations": [v.model_dump() for v in validated.report.violations]},
        )
    patched = validated.patched.get(target_file)
    if patched is None or set(validated.patched) != {target_file} or validated.deleted:
        raise ConflictError("the patch must change exactly the target file")
    data = patched.encode("utf-8")
    if patched_file_sha256 is not None and sha256_bytes(data) != patched_file_sha256:
        raise ConflictError("applying the diff produced a different file than proposed")
    _write_new_inode(src / target_file, data)
    _set_directories_writable(src, False)
    if changed_files(snapshot_root, src) != [target_file]:
        raise ConflictError("the patched copy differs from the snapshot in more than the target")
    verify_original_unchanged(snapshot_root, original_tree_sha256, "after applying the patch")
    return PatchedCopy(
        root=root,
        src=src,
        patched_tree_sha256=hash_tree(src)[0],
        hard_linked_files=linked,
        copied_files=copied,
    )


def verified_patched_copy(src: Path | None, expected_tree_sha256: str | None) -> Path | None:
    """Return the existing patched copy when its tree hash is still the recorded one."""
    if src is None or expected_tree_sha256 is None or not src.is_dir():
        return None
    return src if hash_tree(src)[0] == expected_tree_sha256 else None


def remove_candidate_workspace(settings: Settings, candidate_id: str) -> None:
    _remove(candidate_root(settings, candidate_id))
