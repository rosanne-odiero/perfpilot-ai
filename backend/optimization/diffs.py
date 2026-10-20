"""Strict unified diffs: generation, parsing, and exact (fuzz-free) application.

Only the subset PerfPilot needs is accepted: per-file ``---``/``+++`` headers (optionally led by
``diff --git`` and ``index`` lines), ``@@`` hunks, and ``\\ No newline at end of file``. Anything
else (binary patches, mode changes, renames, copies) is surfaced as a flag or an error so the
patch validator can refuse it. Application never searches for an offset: every hunk must match
the original text exactly at the line it names.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

MAX_DIFF_BYTES = 512 * 1024
NO_NEWLINE_MARKER = "\\ No newline at end of file"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
_INDEX = re.compile(r"^index [0-9a-fA-F]{4,64}\.\.[0-9a-fA-F]{4,64}(?: (\d{6}))?$")
_GIT_HEADER = re.compile(r"^diff --git (\S+) (\S+)$")
_MODE_LINES = ("old mode ", "new mode ", "deleted file mode ", "new file mode ")
_RENAME_LINES = ("similarity index ", "dissimilarity index ", "rename from ", "rename to ")
_COPY_LINES = ("copy from ", "copy to ")
DEV_NULL = "/dev/null"


class DiffParseError(ValueError):
    """The text is not a well-formed unified diff."""


class PatchApplyError(ValueError):
    """A hunk does not match the original text exactly."""


@dataclass
class HunkLine:
    tag: str  # " ", "-", "+"
    text: str
    no_newline: bool = False


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    git_paths: tuple[str, str] | None = None
    mode_lines: list[str] = field(default_factory=list)
    rename_or_copy: list[str] = field(default_factory=list)
    binary: bool = False

    @property
    def is_new_file(self) -> bool:
        return self.old_path == DEV_NULL

    @property
    def is_deleted_file(self) -> bool:
        return self.new_path == DEV_NULL


# ------------------------------------------------------------------------------------------
# Generation
# ------------------------------------------------------------------------------------------


def make_unified_diff(path: str, original: str, patched: str, context: int = 3) -> str:
    """``git``-style unified diff (``a/`` and ``b/`` prefixes) of one text file."""
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        patched.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=context,
    )
    out: list[str] = []
    for line in diff:
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(f"{line}\n{NO_NEWLINE_MARKER}\n")
    return "".join(out)


# ------------------------------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------------------------------


def _header_path(line: str, prefix: str) -> str:
    raw = line[len(prefix) :].split("\t", 1)[0]
    if raw == DEV_NULL:
        return raw
    side = "a/" if prefix == "--- " else "b/"
    if not raw.startswith(side):
        raise DiffParseError(f"file header {line!r} must use the {side!r} prefix or /dev/null")
    path = raw[len(side) :]
    if not path:
        raise DiffParseError(f"file header {line!r} has an empty path")
    return path


def _parse_hunk(lines: list[str], index: int, header: str) -> tuple[Hunk, int]:
    match = _HUNK.match(header)
    if not match:
        raise DiffParseError(f"malformed hunk header {header!r}")
    old_start, old_len, new_start, new_len = (
        int(match.group(1)),
        int(match.group(2) if match.group(2) is not None else 1),
        int(match.group(3)),
        int(match.group(4) if match.group(4) is not None else 1),
    )
    hunk = Hunk(old_start, old_len, new_start, new_len)
    old_seen = new_seen = 0
    while old_seen < old_len or new_seen < new_len:
        if index >= len(lines):
            raise DiffParseError(f"hunk {header!r} is truncated")
        line = lines[index]
        tag, text = line[:1], line[1:]
        if tag not in (" ", "-", "+"):
            raise DiffParseError(f"unexpected line in hunk {header!r}: {line[:80]!r}")
        if tag in (" ", "-"):
            old_seen += 1
        if tag in (" ", "+"):
            new_seen += 1
        if old_seen > old_len or new_seen > new_len:
            raise DiffParseError(f"hunk {header!r} has more lines than its header declares")
        hunk.lines.append(HunkLine(tag, text))
        index += 1
        if index < len(lines) and lines[index] == NO_NEWLINE_MARKER:
            hunk.lines[-1].no_newline = True
            index += 1
    return hunk, index


def parse_unified_diff(text: str) -> list[FilePatch]:
    if len(text.encode("utf-8")) > MAX_DIFF_BYTES:
        raise DiffParseError(f"diff exceeds {MAX_DIFF_BYTES} bytes")
    if "\x00" in text or "\r" in text:
        raise DiffParseError("diff contains NUL or carriage-return characters")
    if not text.endswith("\n"):
        raise DiffParseError("diff must end with a newline")
    lines = text[:-1].split("\n")
    patches: list[FilePatch] = []
    pending_git: tuple[str, str] | None = None
    pending_modes: list[str] = []
    pending_renames: list[str] = []
    pending_binary = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if (git := _GIT_HEADER.match(line)) is not None:
            pending_git, pending_modes, pending_renames = (git.group(1), git.group(2)), [], []
            pending_binary = False
            index += 1
        elif line.startswith(_MODE_LINES):
            pending_modes.append(line)
            index += 1
        elif line.startswith(_RENAME_LINES + _COPY_LINES):
            pending_renames.append(line)
            index += 1
        elif (idx := _INDEX.match(line)) is not None:
            if idx.group(1) is not None:
                pending_modes.append(line)
            index += 1
        elif line == "GIT binary patch" or (line.startswith("Binary files ") and "differ" in line):
            pending_binary = True
            patches.append(
                FilePatch("", "", git_paths=pending_git, mode_lines=pending_modes, binary=True)
            )
            index += 1
            while index < len(lines) and not lines[index].startswith("diff --git "):
                index += 1
        elif line.startswith("--- "):
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise DiffParseError(f"'---' header without '+++' at line {index + 1}")
            patch = FilePatch(
                old_path=_header_path(line, "--- "),
                new_path=_header_path(lines[index + 1], "+++ "),
                git_paths=pending_git,
                mode_lines=pending_modes,
                rename_or_copy=pending_renames,
                binary=pending_binary,
            )
            index += 2
            while index < len(lines) and lines[index].startswith("@@"):
                hunk, index = _parse_hunk(lines, index + 1, lines[index])
                patch.hunks.append(hunk)
            if not patch.hunks:
                raise DiffParseError(f"file {patch.new_path!r} has no hunks")
            patches.append(patch)
            pending_git, pending_modes, pending_renames, pending_binary = None, [], [], False
        else:
            raise DiffParseError(f"unexpected line {index + 1}: {line[:80]!r}")
    if pending_git is not None or pending_modes or pending_renames:
        patches.append(
            FilePatch(
                "",
                "",
                git_paths=pending_git,
                mode_lines=pending_modes,
                rename_or_copy=pending_renames,
            )
        )
    if not patches:
        raise DiffParseError("diff contains no file changes")
    return patches


# ------------------------------------------------------------------------------------------
# Application
# ------------------------------------------------------------------------------------------


def _split(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], True
    ends_with_newline = text.endswith("\n")
    body = text[:-1] if ends_with_newline else text
    return body.split("\n"), ends_with_newline


def apply_file_patch(original: str, patch: FilePatch) -> str:
    """Apply ``patch`` to ``original`` exactly; raise :class:`PatchApplyError` on any mismatch."""
    lines, ends_with_newline = _split(original)
    out: list[str] = []
    out_newline: list[bool] = []  # whether each output line is followed by a newline

    def keep(start: int, stop: int) -> None:
        for position in range(start, stop):
            out.append(lines[position])
            out_newline.append(position < len(lines) - 1 or ends_with_newline)

    cursor = 0  # index into ``lines`` of the next unconsumed original line
    for number, hunk in enumerate(patch.hunks, start=1):
        start = hunk.old_start - 1 if hunk.old_len > 0 else hunk.old_start
        if start < cursor or start > len(lines):
            raise PatchApplyError(f"hunk {number} starts at an invalid or overlapping line")
        keep(cursor, start)
        cursor = start
        for line in hunk.lines:
            if line.tag in (" ", "-"):
                if cursor >= len(lines) or lines[cursor] != line.text:
                    raise PatchApplyError(
                        f"hunk {number} does not match the original at line {cursor + 1}"
                    )
                original_has_newline = cursor < len(lines) - 1 or ends_with_newline
                if line.no_newline == original_has_newline:
                    raise PatchApplyError(
                        f"hunk {number}: 'No newline at end of file' marker does not match "
                        f"the original at line {cursor + 1}"
                    )
                cursor += 1
            if line.tag in (" ", "+"):
                out.append(line.text)
                out_newline.append(not line.no_newline)
    keep(cursor, len(lines))
    if any(not newline for newline in out_newline[:-1]):
        raise PatchApplyError("'No newline at end of file' marker before the last line")
    if not out:
        return ""
    return "\n".join(out) + ("\n" if out_newline[-1] else "")
