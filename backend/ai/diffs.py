"""Unified diffs for mechanical rewrites: generation from real source lines and strict application.

Diffs are always computed with :mod:`difflib` from the exact indexed file text, so every context
line is real source. :func:`apply_unified_diff` re-applies a diff with exact context matching; the
advisor only publishes a diff after it applies cleanly and the result still parses.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

NO_NEWLINE = "\\ No newline at end of file"
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class DiffApplyError(ValueError):
    """The diff is malformed or its context does not match the target text."""


@dataclass(frozen=True)
class Edit:
    """Replace original lines ``start..end`` (1-based, inclusive) with ``lines``.

    ``end == start - 1`` inserts ``lines`` before line ``start`` without removing anything.
    """

    start: int
    end: int
    lines: tuple[str, ...]

    @property
    def is_insertion(self) -> bool:
        return self.end == self.start - 1


def apply_edits(text: str, edits: list[Edit]) -> str:
    """Apply non-overlapping edits computed against the same original ``text``.

    Insertions at the same position keep their given order. Raises ``ValueError`` on overlap.
    """
    lines = text.split("\n")
    ordered = sorted(enumerate(edits), key=lambda pair: (pair[1].start, pair[0]))
    result: list[str] = []
    cursor = 1
    for _, edit in ordered:
        if edit.start < cursor or edit.end > len(lines) or edit.end < edit.start - 1:
            raise ValueError(f"overlapping or out-of-range edit at line {edit.start}")
        result.extend(lines[cursor - 1 : edit.start - 1])
        result.extend(edit.lines)
        cursor = edit.end + 1
    result.extend(lines[cursor - 1 :])
    return "\n".join(result)


def _diff_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def make_unified_diff(path: str, old: str, new: str, context: int = 3) -> str:
    """``git``-style unified diff (``a/`` and ``b/`` prefixes) of two versions of ``path``."""
    out: list[str] = []
    for line in difflib.unified_diff(
        _diff_lines(old), _diff_lines(new), fromfile=f"a/{path}", tofile=f"b/{path}", n=context
    ):
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n")
            out.append(NO_NEWLINE + "\n")
    return "".join(out)


def apply_unified_diff(old: str, diff: str) -> str:
    """Apply a single-file unified diff to ``old`` with exact context matching."""
    source = _diff_lines(old)
    lines = diff.splitlines(keepends=True)
    index = 0
    while index < len(lines) and not lines[index].startswith("@@"):
        if not lines[index].startswith(("--- ", "+++ ", "diff ", "index ")):
            raise DiffApplyError(f"unexpected header line: {lines[index].rstrip()!r}")
        index += 1
    if index == len(lines):
        raise DiffApplyError("diff has no hunks")
    result: list[str] = []
    cursor = 0
    while index < len(lines):
        match = _HUNK.match(lines[index])
        if match is None:
            raise DiffApplyError(f"malformed hunk header: {lines[index].rstrip()!r}")
        old_start = int(match.group(1))
        old_count = int(match.group(2) or "1")
        new_count = int(match.group(4) or "1")
        index += 1
        start = old_start - 1 if old_count else old_start
        if start < cursor:
            raise DiffApplyError("hunks overlap or are out of order")
        result.extend(source[cursor:start])
        cursor = start
        seen_old = seen_new = 0
        while index < len(lines) and not lines[index].startswith("@@"):
            raw = lines[index]
            index += 1
            if raw.rstrip("\n") == NO_NEWLINE:
                raise DiffApplyError("misplaced 'No newline at end of file' marker")
            tag, body = raw[:1], raw[1:]
            if index < len(lines) and lines[index].rstrip("\n") == NO_NEWLINE:
                body = body.removesuffix("\n")
                index += 1
            if tag in {" ", "-"}:
                if cursor >= len(source) or source[cursor] != body:
                    raise DiffApplyError(f"context mismatch at original line {cursor + 1}")
                cursor += 1
                seen_old += 1
                if tag == " ":
                    result.append(body)
                    seen_new += 1
            elif tag == "+":
                result.append(body)
                seen_new += 1
            else:
                raise DiffApplyError(f"unexpected diff line: {raw.rstrip()!r}")
        if seen_old != old_count or seen_new != new_count:
            raise DiffApplyError("hunk line counts do not match its header")
    result.extend(source[cursor:])
    return "".join(result)
