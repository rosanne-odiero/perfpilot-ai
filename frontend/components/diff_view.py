"""Before/after code comparison: side-by-side table and unified diff (pure HTML builders)."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

import streamlit as st

from frontend.components.code_view import highlight_lines
from frontend.theme import esc

RowKind = Literal["equal", "replace", "delete", "insert"]


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
) -> str:
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

    body = "".join(
        f"<tr><td style='width:50%;padding:0'>{cell(r.left_no, left_html, r.kind, 'left')}</td>"
        f"<td style='width:50%;padding:0'>{cell(r.right_no, right_html, r.kind, 'right')}</td></tr>"
        for r in rows
    )
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
