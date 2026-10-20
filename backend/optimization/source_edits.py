"""Formatting-preserving text edits driven by AST node positions.

``ast`` reports ``col_offset`` in UTF-8 *bytes*; :class:`SourceText` converts positions to string
offsets so edits stay correct for non-ASCII source. Edits never overlap; they are applied from
the end of the text backwards so earlier offsets stay valid.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass, field


class EditConflictError(ValueError):
    """Two edits overlap; the transformation is not well-formed."""


class SourceText:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.split("\n")
        self._starts = [0]
        for line in self.lines[:-1]:
            self._starts.append(self._starts[-1] + len(line) + 1)

    def offset(self, lineno: int, col_byte: int) -> int:
        line = self.lines[lineno - 1]
        prefix = line.encode("utf-8")[:col_byte].decode("utf-8")
        return self._starts[lineno - 1] + len(prefix)

    def start(self, node: ast.AST) -> int:
        return self.offset(node.lineno, node.col_offset)  # type: ignore[attr-defined]

    def end(self, node: ast.AST) -> int:
        end_line = node.end_lineno  # type: ignore[attr-defined]
        end_col = node.end_col_offset  # type: ignore[attr-defined]
        return self.offset(end_line, end_col)

    def segment(self, node: ast.AST) -> str:
        return self.text[self.start(node) : self.end(node)]

    def line_start(self, lineno: int) -> int:
        return self._starts[lineno - 1]

    def line_end(self, lineno: int) -> int:
        return self._starts[lineno - 1] + len(self.lines[lineno - 1])

    def indent(self, lineno: int) -> str:
        line = self.lines[lineno - 1]
        return line[: len(line) - len(line.lstrip(" \t"))]


@dataclass
class Edits:
    _items: list[tuple[int, int, int, str]] = field(default_factory=list)

    def replace(self, start: int, end: int, text: str) -> None:
        if end < start:
            raise EditConflictError("edit end precedes its start")
        self._items.append((start, end, len(self._items), text))

    def insert(self, offset: int, text: str) -> None:
        self.replace(offset, offset, text)

    def apply(self, text: str) -> str:
        ordered = sorted(self._items, key=lambda item: (item[0], item[1], item[2]))
        for previous, current in itertools.pairwise(ordered):
            if current[0] < previous[1]:
                raise EditConflictError("overlapping edits")
        result = text
        for start, end, _seq, replacement in reversed(ordered):
            result = result[:start] + replacement + result[end:]
        return result

    def __bool__(self) -> bool:
        return bool(self._items)
