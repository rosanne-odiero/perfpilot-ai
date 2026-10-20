"""Source viewer with line numbers, syntax colours and highlighted line ranges.

Rendering is pure (``code_html``) so it can be unit-tested; ``render_code`` only displays it.
"""

from __future__ import annotations

import builtins
import io
import keyword
import tokenize
from collections.abc import Iterable

import streamlit as st

from frontend.theme import esc

MAX_RENDERED_LINES = 4000
PYTHON_LANGUAGES = {"python", "py"}

KEYWORD = "color:#ff7b72"
STRING = "color:#a5d6ff"
COMMENT = "color:#8b949e;font-style:italic"
NUMBER = "color:#79c0ff"
BUILTIN = "color:#ffa657"
DEFINITION = "color:#d2a8ff;font-weight:600"
_BUILTINS = frozenset(dir(builtins))
_STRING_TOKENS = {tokenize.STRING} | {
    getattr(tokenize, name)
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
    if hasattr(tokenize, name)
}


def _is_python(language: str | None, filename: str | None) -> bool:
    if language:
        return language.lower() in PYTHON_LANGUAGES
    return bool(filename and filename.endswith((".py", ".pyi")))


def _python_spans(source: str) -> list[tuple[int, int, str]]:
    """(start, end, css) character spans for Python tokens worth colouring."""
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def at(position: tuple[int, int]) -> int:
        row, col = position
        return offsets[row - 1] + col if row - 1 < len(offsets) else len(source)

    spans: list[tuple[int, int, str]] = []
    previous = ""
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        style = None
        if token.type in _STRING_TOKENS:
            style = STRING
        elif token.type == tokenize.COMMENT:
            style = COMMENT
        elif token.type == tokenize.NUMBER:
            style = NUMBER
        elif token.type == tokenize.NAME:
            if previous in {"def", "class"} or previous == "@":
                style = DEFINITION
            elif keyword.iskeyword(token.string) or keyword.issoftkeyword(token.string):
                style = KEYWORD
            elif token.string in _BUILTINS:
                style = BUILTIN
        if style:
            spans.append((at(token.start), at(token.end), style))
        if token.type not in {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT}:
            previous = token.string
    return spans


def _render(text: str, css: str | None) -> list[str]:
    """Escaped fragments of ``text`` per line (a span crossing lines is split)."""
    return [
        f'<span style="{css}">{esc(part)}</span>' if css and part else esc(part)
        for part in text.split("\n")
    ]


def highlight_lines(
    source: str, language: str | None = None, filename: str | None = None
) -> list[str]:
    """One HTML fragment per source line. Python is coloured with the stdlib tokenizer;
    anything else (or Python that does not tokenize) is shown as escaped plain text."""
    text = "\n".join(source.expandtabs(4).splitlines())
    spans: list[tuple[int, int, str]] = []
    if _is_python(language, filename):
        try:
            spans = _python_spans(text + "\n")
        except (tokenize.TokenError, SyntaxError, IndentationError):
            spans = []
    lines = [""]
    cursor = 0
    for start, end, css in [*spans, (len(text), len(text), "")]:
        start, end = min(start, len(text)), min(end, len(text))
        if start < cursor:
            continue
        for chunk, style in ((text[cursor:start], None), (text[start:end], css)):
            fragments = _render(chunk, style)
            lines[-1] += fragments[0]
            lines.extend(fragments[1:])
        cursor = end
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
