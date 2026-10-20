"""Safe ZIP extraction.

The whole central directory is validated before anything is written: absolute paths, ``..``,
drive letters, backslashes, control characters, symlinks, device/special files, encrypted
entries, duplicate (case-insensitive) names, file/directory collisions, entry counts and
compression ratios are all rejected up front. Extraction then streams every entry and counts the
bytes actually produced, so a lying header cannot inflate a file beyond its declared size.
"""

from __future__ import annotations

import os
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from backend.core.config import Settings
from backend.core.exceptions import (
    LimitExceededError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.ingestion.errors import UnsafeArchiveError
from backend.ingestion.filters import SkipReason, classify_dir, classify_file
from backend.ingestion.limits import IngestLimits, LimitTracker
from backend.ingestion.walk import skipped_file_info
from backend.models.audit_schemas import SourceFileInfo

RATIO_MIN_BYTES = 1 << 20
"""Compression-ratio checks apply once (declared or actual) sizes exceed this many bytes."""

_CHUNK = 64 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNIX = 3


@dataclass
class ExtractionResult:
    root_name: str | None = None
    skipped: list[SourceFileInfo] = field(default_factory=list)
    limits_hit: list[str] = field(default_factory=list)
    files_extracted: int = 0
    bytes_extracted: int = 0


@dataclass(frozen=True)
class _Member:
    info: zipfile.ZipInfo
    parts: tuple[str, ...]
    is_dir: bool

    @property
    def rel(self) -> str:
        return "/".join(self.parts)


def normalize_member_name(name: str) -> tuple[str, ...]:
    """Split an archive member name into safe path components or raise UnsafeArchiveError."""
    if "\\" in name:
        raise UnsafeArchiveError("archive entry uses backslashes", details={"entry": name[:200]})
    if _CONTROL.search(name):
        raise UnsafeArchiveError("archive entry name contains control characters")
    if name.startswith("/") or _DRIVE.match(name):
        raise UnsafeArchiveError(
            "archive entry has an absolute path", details={"entry": name[:200]}
        )
    parts = tuple(p for p in name.split("/") if p not in ("", "."))
    if ".." in parts:
        raise UnsafeArchiveError(
            "archive entry escapes the archive root", details={"entry": name[:200]}
        )
    return parts


def _check_entry_type(info: zipfile.ZipInfo) -> bool:
    """Return True for directories; raise for symlinks, special files and encrypted entries."""
    if info.flag_bits & 0x1:
        raise UnsafeArchiveError("encrypted archive entries are not supported")
    mode = info.external_attr >> 16
    if info.create_system == _UNIX and mode:
        if stat.S_ISLNK(mode):
            raise UnsafeArchiveError(
                "archive contains a symbolic link", details={"entry": info.filename[:200]}
            )
        if stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise UnsafeArchiveError(
                "archive contains a device or special file", details={"entry": info.filename[:200]}
            )
        if stat.S_ISDIR(mode):
            return True
    return info.is_dir()


def _check_ratio(size: int, compressed: int, max_ratio: float, entry: str) -> None:
    if size < RATIO_MIN_BYTES:
        return
    if compressed <= 0 or size / compressed > max_ratio:
        raise UnsafeArchiveError(
            "archive compression ratio exceeds the zip-bomb guard",
            details={"entry": entry[:200], "max_ratio": max_ratio},
        )


def _validate_members(zf: zipfile.ZipFile, settings: Settings) -> list[_Member]:
    infos = zf.infolist()
    if len(infos) > settings.max_archive_entries:
        raise LimitExceededError(
            f"archive has {len(infos)} entries; the limit is {settings.max_archive_entries}"
        )
    members: list[_Member] = []
    total_size = total_compressed = 0
    for info in infos:
        is_dir = _check_entry_type(info)
        parts = normalize_member_name(info.filename)
        if not parts:
            continue
        if not is_dir:
            _check_ratio(
                info.file_size, info.compress_size, settings.max_compression_ratio, info.filename
            )
            total_size += info.file_size
            total_compressed += info.compress_size
        members.append(_Member(info, parts, is_dir))
    _check_ratio(total_size, total_compressed, settings.max_compression_ratio, "<archive>")

    seen: set[str] = set()
    prefixes: set[str] = set()
    for member in members:
        key = member.rel.casefold()
        if not member.is_dir:
            if key in seen:
                raise UnsafeArchiveError(
                    "archive contains duplicate entries", details={"entry": member.rel[:200]}
                )
            seen.add(key)
        for i in range(1, len(member.parts) + (1 if member.is_dir else 0)):
            prefixes.add("/".join(member.parts[:i]).casefold())
    if seen & prefixes:
        raise UnsafeArchiveError("archive contains an entry that is both a file and a directory")
    return members


def _unwrap(members: list[_Member]) -> tuple[str | None, list[_Member]]:
    """Strip a single top-level directory shared by every entry (common for GitHub ZIPs)."""
    files = [m for m in members if not m.is_dir]
    if not files or any(len(m.parts) < 2 for m in files):
        return None, members
    top = files[0].parts[0]
    if any(m.parts[0] != top for m in members):
        return None, members
    stripped = [_Member(m.info, m.parts[1:], m.is_dir) for m in members if len(m.parts) > 1]
    return top, stripped


def _extract_member(
    zf: zipfile.ZipFile, member: _Member, dest_root: Path, settings: Settings
) -> int:
    target = dest_root.joinpath(*member.parts)
    if not target.resolve().is_relative_to(dest_root):
        raise SecurityViolationError("archive entry escapes the workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    declared = member.info.file_size
    limit = min(declared, settings.max_file_bytes)
    written = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644)
    try:
        with zf.open(member.info) as source:
            while chunk := source.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise UnsafeArchiveError(
                        "archive entry decompresses beyond its declared size",
                        details={"entry": member.rel[:200]},
                    )
                _check_ratio(
                    written,
                    member.info.compress_size,
                    settings.max_compression_ratio,
                    member.rel,
                )
                os.write(fd, chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
        raise UnsafeArchiveError(
            "archive entry is corrupt or its header is inconsistent",
            details={"entry": member.rel[:200]},
        ) from exc
    finally:
        os.close(fd)
    return written


def safe_extract_zip(archive: Path, dest: Path, settings: Settings) -> ExtractionResult:
    """Validate and extract ``archive`` into the empty directory ``dest`` with filtering/limits."""
    dest_root = dest.resolve()
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValidationFailedError("upload is not a valid ZIP archive") from exc
    with zf:
        members = _validate_members(zf, settings)
        result = ExtractionResult()
        macos = [m for m in members if m.parts[0] == "__MACOSX"]
        if macos:
            result.skipped.append(skipped_file_info("__MACOSX/", 0, SkipReason.OS_METADATA))
            members = [m for m in members if m.parts[0] != "__MACOSX"]
        result.root_name, members = _unwrap(members)

        paths = {m.rel for m in members}
        dirs = {"/".join(m.parts[:i]) for m in members for i in range(1, len(m.parts))}
        dirs |= {m.rel for m in members if m.is_dir}

        def has_child(directory: str, name: str) -> bool:
            child = f"{directory}/{name}"
            return child in paths or child in dirs

        pruned: dict[str, SkipReason] = {}

        def under_pruned(parts: tuple[str, ...]) -> bool:
            return any("/".join(parts[:i]) in pruned for i in range(1, len(parts)))

        for directory in sorted(dirs, key=lambda d: d.split("/")):
            if under_pruned(tuple(directory.split("/"))):
                continue
            name = directory.rsplit("/", 1)[-1]
            reason = classify_dir(name, partial(has_child, directory))
            if reason is not None:
                pruned[directory] = reason
                result.skipped.append(skipped_file_info(f"{directory}/", 0, reason))

        tracker = LimitTracker(IngestLimits.from_settings(settings))
        for member in sorted((m for m in members if not m.is_dir), key=lambda m: m.parts):
            if under_pruned(member.parts):
                continue
            size = member.info.file_size
            reason = classify_file(member.rel, size, settings.max_file_bytes)
            if not tracker.admit(size, kept=reason is None):
                break
            if reason is not None:
                result.skipped.append(skipped_file_info(member.rel, size, reason))
                continue
            result.bytes_extracted += _extract_member(zf, member, dest_root, settings)
            result.files_extracted += 1
        result.limits_hit = list(tracker.hit)
    return result
