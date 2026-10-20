"""Deterministic, symlink-safe directory walk; filters and limits apply *during* the walk."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from backend.ingestion.filters import SkipReason, classify_dir, classify_file
from backend.ingestion.languages import is_python_path, language_for
from backend.ingestion.limits import LimitTracker
from backend.models.audit_schemas import SourceFileInfo


@dataclass(frozen=True)
class TreeEntry:
    rel_path: str
    """POSIX path relative to the walk root; pruned directories end with ``/``."""
    path: Path
    is_dir: bool
    size: int
    skip_reason: SkipReason | None = None

    def skipped_info(self) -> SourceFileInfo:
        return skipped_file_info(
            self.rel_path, self.size, self.skip_reason or SkipReason.UNREADABLE
        )


def skipped_file_info(rel_path: str, size: int, reason: SkipReason) -> SourceFileInfo:
    """Inventory record for a file or pruned directory (``rel_path`` ending in ``/``)."""
    is_dir = rel_path.endswith("/")
    return SourceFileInfo(
        path=rel_path,
        size_bytes=size,
        language=None if is_dir else language_for(rel_path),
        is_python=False if is_dir else is_python_path(rel_path),
        skipped=True,
        skip_reason=reason.value,
    )


def _has_child(parent: Path, name: str) -> bool:
    return os.path.lexists(parent / name)


def walk_tree(
    root: Path,
    tracker: LimitTracker,
    *,
    excluded_dirs: Iterable[Path] = (),
) -> Iterator[TreeEntry]:
    """Yield files (kept or skipped) and pruned directories under ``root``.

    Symlinks are never followed, pruned directories are never descended into, entries are
    visited in sorted order, and the walk stops as soon as ``tracker`` reports a limit.
    """
    excluded = {p.resolve() for p in excluded_dirs}
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            if prefix:
                yield TreeEntry(prefix, directory, True, 0, SkipReason.UNREADABLE)
            continue
        subdirs: list[tuple[Path, str]] = []
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            path = Path(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                if not tracker.admit(0, kept=False):
                    return
                yield TreeEntry(rel, path, False, 0, SkipReason.UNREADABLE)
                continue
            if stat.S_ISLNK(st.st_mode):
                if not tracker.admit(0, kept=False):
                    return
                yield TreeEntry(rel, path, False, 0, SkipReason.SYMLINK)
            elif stat.S_ISDIR(st.st_mode):
                reason: SkipReason | None
                if path in excluded:
                    reason = SkipReason.PERFPILOT_DATA
                else:
                    reason = classify_dir(entry.name, partial(_has_child, path))
                if reason is not None:
                    yield TreeEntry(f"{rel}/", path, True, 0, reason)
                else:
                    subdirs.append((path, f"{rel}/"))
            elif stat.S_ISREG(st.st_mode):
                reason = classify_file(rel, st.st_size, tracker.limits.max_file_bytes)
                if not tracker.admit(st.st_size, kept=reason is None):
                    return
                yield TreeEntry(rel, path, False, st.st_size, reason)
            else:
                if not tracker.admit(0, kept=False):
                    return
                yield TreeEntry(rel, path, False, 0, SkipReason.SPECIAL_FILE)
        # Reverse so the stack pops subdirectories in sorted order (depth-first, deterministic).
        stack.extend(reversed(subdirs))
