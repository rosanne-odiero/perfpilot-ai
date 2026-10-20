"""Content hashing of source trees (``SourceSnapshot`` rows)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy.orm import Session

from backend.infrastructure.artifact_store import sha256_file
from backend.models.orm.workloads import SourceSnapshot

IGNORED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git"})
IGNORED_SUFFIXES = (".pyc", ".pyo")


def hash_tree(root: Path) -> tuple[str, dict[str, str]]:
    """Return ``(tree_sha256, {relative_path: sha256})``. Symlinks are hashed by their target
    string and never followed, so a link cannot pull outside content into the snapshot."""
    root = root.resolve()
    files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            if name.endswith(IGNORED_SUFFIXES):
                continue
            path = Path(dirpath) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                files[relative] = "symlink:" + hashlib.sha256(target.encode()).hexdigest()
            elif path.is_file():
                files[relative] = sha256_file(path)
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(f"{relative}\0{files[relative]}\n".encode())
    return digest.hexdigest(), files


def create_snapshot(
    session: Session, *, workload_id: str, role: str, label: str, root: Path
) -> SourceSnapshot:
    tree_sha, files = hash_tree(root)
    snapshot = SourceSnapshot(
        workload_id=workload_id,
        role=role,
        label=label,
        root_path=str(root.resolve()),
        tree_sha256=tree_sha,
        files=files,
    )
    session.add(snapshot)
    session.flush()
    return snapshot
