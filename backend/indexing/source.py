"""Reading and parsing repository source files without importing or executing them."""

from __future__ import annotations

import ast
import io
import tokenize
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from backend.indexing.names import ImportAliases


@dataclass
class ParsedModule:
    """One Python file: normalized text, its lines, and the AST (``None`` on parse errors).

    Line numbers in ``tree`` index into ``lines`` (1-based), because both come from the same
    newline-normalized text.
    """

    path: str
    source: str
    lines: list[str]
    tree: ast.Module | None
    error: str | None = None
    decode_warning: str | None = None
    aliases: ImportAliases = field(default_factory=ImportAliases)
    module_name: str = ""

    @property
    def is_package(self) -> bool:
        return self.path.rsplit("/", 1)[-1] == "__init__.py"

    def snippet(self, line_start: int, line_end: int) -> str:
        return "\n".join(self.lines[line_start - 1 : line_end])


def decode_source(raw: bytes) -> tuple[str, str | None]:
    """Decode bytes using the PEP 263 declaration (BOM aware); fall back to lossy UTF-8."""
    warning: str | None = None
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except SyntaxError:
        encoding = "utf-8"
        warning = "invalid encoding declaration; decoded as UTF-8"
    try:
        text = raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("utf-8", errors="replace")
        warning = f"could not decode as {encoding}; undecodable bytes were replaced"
    return text.replace("\r\n", "\n").replace("\r", "\n"), warning


def parse_source(path: str, text: str, decode_warning: str | None = None) -> ParsedModule:
    lines = text.split("\n")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # SyntaxWarning for invalid escapes in user code
            tree = ast.parse(text, filename=path, type_comments=False)
    except SyntaxError as exc:
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return ParsedModule(path, text, lines, None, f"SyntaxError at {where}: {exc.msg}")
    except (ValueError, RecursionError, MemoryError) as exc:
        return ParsedModule(path, text, lines, None, f"{type(exc).__name__}: {exc}")
    return ParsedModule(
        path, text, lines, tree, decode_warning=decode_warning, aliases=ImportAliases.build(tree)
    )


def parse_file(root: Path, rel_path: str) -> ParsedModule:
    """Read and parse ``root / rel_path``. I/O errors become a recorded parse error."""
    try:
        raw = (root / rel_path).read_bytes()
    except OSError as exc:
        return ParsedModule(rel_path, "", [], None, f"unreadable: {exc.strerror or exc}")
    text, warning = decode_source(raw)
    return parse_source(rel_path, text, warning)
