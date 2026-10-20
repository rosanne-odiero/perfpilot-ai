"""Build a :class:`RepositoryInventory` from an acquired working copy."""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from backend.core.config import Settings
from backend.ingestion.dependencies import discover_dependencies
from backend.ingestion.detection import TechnologyDetector
from backend.ingestion.filters import SkipReason, classify_content
from backend.ingestion.languages import is_python_path, language_for
from backend.ingestion.limits import IngestLimits, LimitTracker
from backend.ingestion.walk import TreeEntry, skipped_file_info, walk_tree
from backend.models.audit_schemas import RepositoryInventory, SourceFileInfo

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def read_capped(path: Path, max_bytes: int) -> bytes | None:
    """Read at most ``max_bytes`` without following symlinks; None if the file is larger."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _inspect(
    entry: TreeEntry, tracker: LimitTracker, detector: TechnologyDetector
) -> SourceFileInfo:
    rel = entry.rel_path
    try:
        data = read_capped(entry.path, tracker.limits.max_file_bytes)
    except OSError:
        return skipped_file_info(rel, entry.size, SkipReason.UNREADABLE)
    if data is None:
        return skipped_file_info(rel, entry.size, SkipReason.OVERSIZED)
    is_python = is_python_path(rel)
    reason = classify_content(rel, data)
    if reason is None and is_python and not tracker.admit_python():
        reason = SkipReason.PYTHON_FILE_LIMIT
    if reason is None and is_python:
        detector.observe_python(rel, data)
    return SourceFileInfo(
        path=rel,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        language=language_for(rel),
        is_python=is_python,
        skipped=reason is not None,
        skip_reason=reason.value if reason else None,
    )


def build_inventory(
    root: Path,
    settings: Settings,
    *,
    prefiltered: Sequence[SourceFileInfo] = (),
    limits_hit: Sequence[str] = (),
    root_name: str | None = None,
) -> RepositoryInventory:
    """Walk ``root`` and classify, hash and analyze every file.

    ``prefiltered`` and ``limits_hit`` carry what the provider already filtered out while
    acquiring (e.g. ``node_modules/`` never copied from a local directory), so every provider
    yields the same inventory for the same tree. Sensitive files are never read or hashed.
    """
    root = root.resolve()
    tracker = LimitTracker(IngestLimits.from_settings(settings))
    detector = TechnologyDetector()
    files: dict[str, SourceFileInfo] = {f.path: f for f in prefiltered}
    for entry in walk_tree(root, tracker):
        info = entry.skipped_info() if entry.skip_reason else _inspect(entry, tracker, detector)
        files.setdefault(info.path, info)
    ordered = sorted(files.values(), key=lambda f: f.path)
    file_entries = [f for f in ordered if not f.path.endswith("/")]
    kept = [f for f in file_entries if not f.skipped]
    languages = Counter(f.language for f in kept if f.language)

    dependencies = discover_dependencies(root, (f.path for f in kept))
    detector.observe_languages(dict(sorted(languages.items())))
    detector.observe_files(root, (f.path for f in kept), (f.path for f in file_entries))
    detector.observe_dependencies(dependencies)

    return RepositoryInventory(
        root_name=root_name or root.name,
        files=ordered,
        total_files_seen=len(file_entries),
        total_bytes_seen=sum(f.size_bytes for f in file_entries),
        languages=dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))),
        technologies=detector.result(),
        dependencies=dependencies,
        limits_hit=list(dict.fromkeys([*limits_hit, *tracker.hit])),
    )


def inventory_content_hash(inventory: RepositoryInventory) -> str:
    """Stable hash of the analyzed content: kept file paths and their sha256 digests."""
    digest = hashlib.sha256()
    for info in inventory.files:
        if not info.skipped and info.sha256:
            digest.update(f"{info.path}\0{info.sha256}\n".encode())
    return digest.hexdigest()


def skipped_counts(inventory: RepositoryInventory) -> dict[str, int]:
    counts = Counter(f.skip_reason for f in inventory.files if f.skipped and f.skip_reason)
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
