"""Isolated candidate workspaces under ``<data_dir>/workspaces/optimizations/<candidate_id>``.

The original workload tree is only ever *read*: it is copied twice (``baseline/`` and
``patched/``), the validated diff is applied to the ``patched/`` copy, and the original tree's
hash is re-checked after every step so an accidental write is caught immediately.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, SecurityViolationError, ValidationFailedError
from backend.infrastructure.artifact_store import sha256_bytes
from backend.optimization.patch_validator import validate_patch
from backend.services.source_snapshots import IGNORED_DIRS, IGNORED_SUFFIXES, hash_tree

_CANDIDATE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SLUG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class OriginalTreeChangedError(ConflictError):
    code = "original_tree_changed"


class PreparedWorkspace(BaseModel):
    root: Path
    baseline_root: Path
    patched_root: Path
    original_tree_sha256: str
    patched_tree_sha256: str


def candidate_dir(settings: Settings, candidate_id: str) -> Path:
    if not _CANDIDATE_ID.match(candidate_id):
        raise SecurityViolationError("invalid candidate id")
    base = (settings.workspaces_dir / "optimizations").resolve()
    path = (base / candidate_id).resolve()
    if not path.is_relative_to(base):
        raise SecurityViolationError("candidate workspace escapes the workspaces directory")
    return path


def verify_original_unchanged(root: Path, expected_tree_sha256: str, when: str) -> str:
    actual, _ = hash_tree(root)
    if actual != expected_tree_sha256:
        raise OriginalTreeChangedError(
            f"the original source tree changed ({when}); refusing to continue",
            details={"expected_tree_sha256": expected_tree_sha256, "actual": actual},
        )
    return actual


def _copy(source: Path, dest: Path) -> None:
    shutil.copytree(
        source,
        dest,
        symlinks=True,
        ignore=shutil.ignore_patterns(*IGNORED_DIRS, *(f"*{s}" for s in IGNORED_SUFFIXES)),
    )


def prepare_workspace(
    settings: Settings,
    *,
    candidate_id: str,
    slug: str,
    original_root: Path,
    original_tree_sha256: str,
    diff: str,
    allowed_files: Sequence[str],
    patched_file_sha256: str | None,
    extra_protected: Sequence[str] = (),
) -> PreparedWorkspace:
    """Fresh ``baseline``/``patched`` copies with the diff applied to the patched one."""
    if not _SLUG.match(slug):
        raise SecurityViolationError("invalid workload slug")
    original_root = original_root.resolve()
    verify_original_unchanged(original_root, original_tree_sha256, "before copying")
    root = candidate_dir(settings, candidate_id)
    if root.exists():
        shutil.rmtree(root)
    baseline_root, patched_root = root / "baseline" / slug, root / "patched" / slug
    _copy(original_root, baseline_root)
    _copy(original_root, patched_root)
    for copy in (baseline_root, patched_root):
        if hash_tree(copy)[0] != original_tree_sha256:
            raise ConflictError("the workspace copy does not match the original tree")
    validated = validate_patch(
        diff,
        workspace_root=patched_root,
        allowed_files=allowed_files,
        extra_protected=extra_protected,
    )
    if not validated.report.valid:
        raise ValidationFailedError(
            "the patch no longer passes the patch validator",
            details={"violations": [v.model_dump() for v in validated.report.violations]},
        )
    for relative, text in validated.patched.items():
        data = text.encode("utf-8")
        if patched_file_sha256 is not None and sha256_bytes(data) != patched_file_sha256:
            raise ConflictError("applying the diff produced a different file than proposed")
        (patched_root / relative).write_bytes(data)
    for relative in validated.deleted:
        (patched_root / relative).unlink()
    patched_tree, _ = hash_tree(patched_root)
    verify_original_unchanged(original_root, original_tree_sha256, "after applying the patch")
    return PreparedWorkspace(
        root=root,
        baseline_root=baseline_root,
        patched_root=patched_root,
        original_tree_sha256=original_tree_sha256,
        patched_tree_sha256=patched_tree,
    )


def changed_files(baseline_root: Path, patched_root: Path) -> list[str]:
    _, before = hash_tree(baseline_root)
    _, after = hash_tree(patched_root)
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
