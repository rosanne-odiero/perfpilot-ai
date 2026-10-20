"""Source viewer with line numbers, syntax colours and highlighted line ranges.

Rendering is pure (``code_html``) so it can be unit-tested; ``render_code`` only displays it.
"""

from __future__ import annotations

from collections.abc import Iterable

import streamlit as st
from pygments.lexer import Lexer
from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.styles import get_style_by_name
from pygments.token import _TokenType
from pygments.util import ClassNotFound

from frontend.theme import esc

STYLE = get_style_by_name("github-dark")
MAX_RENDERED_LINES = 4000


def _lexer(language: str | None, filename: str | None) -> Lexer:
    if language:
        try:
            return get_lexer_by_name(language.lower(), stripnl=False, ensurenl=False)
        except ClassNotFound:
            pass
    if filename:
        try:
            return get_lexer_for_filename(filename, stripnl=False, ensurenl=False)
        except ClassNotFound:
            pass
    return TextLexer(stripnl=False, ensurenl=False)


def _span(token: _TokenType, text: str) -> str:
    if not text:
        return ""
    style = STYLE.style_for_token(token)
    css = []
    if style["color"]:
        css.append(f"color:#{style['color']}")
    if style["bold"]:
        css.append("font-weight:600")
    if style["italic"]:
        css.append("font-style:italic")
    escaped = esc(text)
    return f'<span style="{";".join(css)}">{escaped}</span>' if css else escaped


def highlight_lines(
    source: str, language: str | None = None, filename: str | None = None
) -> list[str]:
    """One HTML fragment per source line (tokens spanning lines are split per line)."""
    lines: list[str] = [""]
    for token, value in _lexer(language, filename).get_tokens(source.expandtabs(4)):
        parts = value.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append("")
            lines[-1] += _span(token, part)
    if source.endswith("\n") and len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def context_window(start: int, end: int, total: int, context: int = 6) -> tuple[int, int]:
    """1-based inclusive line window around ``start..end``, clamped to ``1..total``."""
    first = max(1, min(start, end) - context)
    last = min(total, max(start, end) + context)
    return first, max(first, last)


def code_html(
    source: str,
    *,
    language: str | None = None,
    filename: str | None = None,
    first_line: int = 1,
    highlight: Iterable[int] = (),
    window: tuple[int, int] | None = None,
    title: str | None = None,
    note: str | None = None,
) -> str:
    """Table of numbered lines. ``first_line`` is the number of the first line of ``source``;
    ``window`` (absolute, inclusive) limits which lines are shown."""
    lines = highlight_lines(source, language, filename)
    marked = set(highlight)
    numbered = list(enumerate(lines, start=first_line))
    if window is not None:
        numbered = [(n, h) for n, h in numbered if window[0] <= n <= window[1]]
    truncated = len(numbered) > MAX_RENDERED_LINES
    numbered = numbered[:MAX_RENDERED_LINES]
    rows = "".join(
        f'<tr class="{"pp-hl" if number in marked else ""}"><td class="pp-ln">{number}</td>'
        f'<td class="pp-src"><pre>{text or " "}</pre></td></tr>'
        for number, text in numbered
    )
    head = ""
    if title or note or truncated:
        right = esc(note) if note else ""
        if truncated:
            right += f" · first {MAX_RENDERED_LINES} lines shown"
        head = (
            f'<div class="pp-code-head"><span>{esc(title or "")}</span><span>{right}</span></div>'
        )
    return f'<div class="pp-code">{head}<table>{rows}</table></div>'


def render_code(
    source: str,
    *,
    language: str | None = None,
    filename: str | None = None,
    first_line: int = 1,
    highlight: Iterable[int] = (),
    window: tuple[int, int] | None = None,
    title: str | None = None,
    note: str | None = None,
) -> None:
    st.html(
        code_html(
            source,
            language=language,
            filename=filename,
            first_line=first_line,
            highlight=highlight,
            window=window,
            title=title,
            note=note,
        )
    )
