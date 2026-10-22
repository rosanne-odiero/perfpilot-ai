"""Before/after code comparison: side-by-side table and unified diff (pure HTML builders)."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Literal

import streamlit as st

from frontend.components.code_view import highlight_lines
from frontend.theme import esc

RowKind = Literal["equal", "replace", "delete", "insert"]
UnifiedKind = Literal["file", "hunk", "add", "del", "context", "meta"]
_HUNK = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_len>\d+))? \+(?P<new>\d+)(?:,(?P<new_len>\d+))? @@"
)


@dataclass(frozen=True)
class DiffRow:
    kind: RowKind
    left_no: int | None
    left: str | None
    right_no: int | None
    right: str | None


def side_by_side_rows(before: str, after: str) -> list[DiffRow]:
    """Align lines of ``before`` and ``after``; ``left``/``right`` hold raw text."""
    a, b = before.splitlines(), after.splitlines()
    rows: list[DiffRow] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows.extend(
                DiffRow("equal", i + 1, a[i], j + 1, b[j])
                for i, j in zip(range(i1, i2), range(j1, j2), strict=True)
            )
            continue
        left, right = list(range(i1, i2)), list(range(j1, j2))
        for k in range(max(len(left), len(right))):
            li = left[k] if k < len(left) else None
            rj = right[k] if k < len(right) else None
            if li is not None and rj is not None:
                kind: RowKind = "replace"
            else:
                kind = "delete" if rj is None else "insert"
            rows.append(
                DiffRow(
                    kind,
                    None if li is None else li + 1,
                    None if li is None else a[li],
                    None if rj is None else rj + 1,
                    None if rj is None else b[rj],
                )
            )
    return rows


DEFAULT_CONTEXT = 4


def fold_unchanged(rows: list[DiffRow], context: int = DEFAULT_CONTEXT) -> list[DiffRow | int]:
    """Keep changed rows plus ``context`` unchanged rows around them; runs of other unchanged
    rows become a single ``int`` (how many lines were hidden)."""
    changed = [i for i, row in enumerate(rows) if row.kind != "equal"]
    if not changed:
        return list(rows)
    keep = set()
    for i in changed:
        keep.update(range(max(0, i - context), min(len(rows), i + context + 1)))
    folded: list[DiffRow | int] = []
    hidden = 0
    for i, row in enumerate(rows):
        if i in keep:
            if hidden:
                folded.append(hidden)
                hidden = 0
            folded.append(row)
        else:
            hidden += 1
    if hidden:
        folded.append(hidden)
    return folded


def unified_diff(before: str, after: str, before_name: str, after_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=before_name,
            tofile=after_name,
        )
    )


def diff_stats(rows: list[DiffRow]) -> tuple[int, int]:
    """(lines added, lines removed)."""
    added = sum(1 for r in rows if r.right is not None and r.kind in {"insert", "replace"})
    removed = sum(1 for r in rows if r.left is not None and r.kind in {"delete", "replace"})
    return added, removed


def _cell_class(kind: RowKind, side: Literal["left", "right"], present: bool) -> str:
    if not present:
        return "pp-diff-empty"
    if kind == "equal":
        return ""
    if side == "left":
        return "pp-diff-del" if kind in {"delete", "replace"} else ""
    return "pp-diff-add" if kind in {"insert", "replace"} else ""


def side_by_side_html(
    before: str,
    after: str,
    *,
    before_title: str,
    after_title: str,
    language: str | None = "python",
    context: int | None = DEFAULT_CONTEXT,
) -> str:
    """Side-by-side table; with ``context`` set, unchanged stretches are folded."""
    left_html = highlight_lines(before, language)
    right_html = highlight_lines(after, language)
    rows = side_by_side_rows(before, after)

    def cell(
        number: int | None, lines: list[str], kind: RowKind, side: Literal["left", "right"]
    ) -> str:
        cls = _cell_class(kind, side, number is not None)
        body = lines[number - 1] if number is not None and number - 1 < len(lines) else ""
        return (
            f'<table><tr class="{cls}"><td class="pp-ln">{number or ""}</td>'
            f'<td class="pp-src"><pre>{body or " "}</pre></td></tr></table>'
        )

    def row_html(item: DiffRow | int) -> str:
        if isinstance(item, int):
            return (
                "<tr><td colspan='2' class='pp-diff-fold' style='text-align:center;opacity:.6'>"
                f"⋯ {item} unchanged line{'s' if item != 1 else ''} ⋯</td></tr>"
            )
        left = cell(item.left_no, left_html, item.kind, "left")
        right = cell(item.right_no, right_html, item.kind, "right")
        return (
            f"<tr><td style='width:50%;padding:0'>{left}</td>"
            f"<td style='width:50%;padding:0'>{right}</td></tr>"
        )

    shown: list[DiffRow | int] = list(rows) if context is None else fold_unchanged(rows, context)
    body = "".join(row_html(item) for item in shown)
    added, removed = diff_stats(rows)
    head = (
        '<div class="pp-code-head">'
        f'<span class="pp-diff-head-before">◀ {esc(before_title)}</span>'
        f"<span>+{added} / -{removed} lines</span>"
        f'<span class="pp-diff-head-after">{esc(after_title)} ▶</span></div>'
    )
    return f'<div class="pp-code">{head}<table>{body}</table></div>'


def render_side_by_side(
    before: str, after: str, *, before_title: str, after_title: str, language: str = "python"
) -> None:
    st.html(
        side_by_side_html(
            before, after, before_title=before_title, after_title=after_title, language=language
        )
    )


@dataclass(frozen=True)
class UnifiedLine:
    kind: UnifiedKind
    old_no: int | None
    new_no: int | None
    text: str


def parse_unified_diff(diff: str) -> list[UnifiedLine]:
    """Classify each line of a unified diff and number it in the old and new file.

    Hunk bodies are delimited by the hunk header counts, so a removed line that starts with
    ``-- `` is never mistaken for a file header."""
    lines: list[UnifiedLine] = []
    old = new = old_left = new_left = 0
    for raw in diff.splitlines():
        in_hunk = old_left > 0 or new_left > 0
        if not in_hunk and (match := _HUNK.match(raw)):
            old, new = int(match["old"]), int(match["new"])
            old_left = int(match["old_len"] or 1)
            new_left = int(match["new_len"] or 1)
            lines.append(UnifiedLine("hunk", None, None, raw))
        elif raw.startswith("\\"):
            lines.append(UnifiedLine("meta", None, None, raw))
        elif not in_hunk:
            lines.append(UnifiedLine("file", None, None, raw))
        elif raw.startswith("+"):
            lines.append(UnifiedLine("add", None, new, raw[1:]))
            new, new_left = new + 1, new_left - 1
        elif raw.startswith("-"):
            lines.append(UnifiedLine("del", old, None, raw[1:]))
            old, old_left = old + 1, old_left - 1
        else:
            lines.append(UnifiedLine("context", old, new, raw[1:]))
            old, new = old + 1, new + 1
            old_left, new_left = old_left - 1, new_left - 1
    return lines


_UNIFIED_CLASS = {"add": "pp-diff-add", "del": "pp-diff-del", "hunk": "pp-diff-hunk"}
_UNIFIED_SIGN = {"add": "+", "del": "-"}


def unified_diff_html(diff: str, title: str | None = None) -> str:
    parsed = parse_unified_diff(diff)
    added = sum(1 for line in parsed if line.kind == "add")
    removed = sum(1 for line in parsed if line.kind == "del")
    rows = "".join(
        f'<tr class="{_UNIFIED_CLASS.get(line.kind, "")}">'
        f'<td class="pp-ln">{line.old_no or ""}</td><td class="pp-ln">{line.new_no or ""}</td>'
        f'<td class="pp-src"><pre>{_UNIFIED_SIGN.get(line.kind, " ")}{esc(line.text) or " "}'
        "</pre></td></tr>"
        for line in parsed
        if line.kind != "file"
    )
    files = ", ".join(
        esc(line.text[4:].removeprefix("b/"))
        for line in parsed
        if line.kind == "file" and line.text.startswith("+++ ")
    )
    head = (
        f'<div class="pp-code-head"><span>{esc(title) if title else files}</span>'
        f"<span>+{added} / -{removed} lines</span></div>"
    )
    return f'<div class="pp-code">{head}<table>{rows}</table></div>'


def render_unified(diff: str, title: str | None = None) -> None:
    st.html(unified_diff_html(diff, title))
