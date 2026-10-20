"""Source references for reports: locations, GitHub permalinks, snippets and diffs."""

from __future__ import annotations

import re
from urllib.parse import quote

from backend.core.redaction import is_sensitive_path
from backend.models.audit_schemas import CodeLocation
from backend.models.enums import RepositorySourceType
from backend.reporting.model import SnippetLine

MAX_SNIPPET_LINES = 40
MAX_DIFF_LINES = 400
SENSITIVE_WITHHELD = "Content withheld: the file matches a sensitive-file pattern."

_GITHUB_REPO = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:/tree/[^\s?#]+)?/?$"
)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_DIFF_TARGET = re.compile(r"^(?:\+\+\+|---) (?:[ab]/)?(?P<path>\S+)")
_GITHUB_SOURCES = {RepositorySourceType.GITHUB_PUBLIC, RepositorySourceType.GITHUB_PRIVATE}


def format_location(location: CodeLocation) -> str:
    if location.line_end > location.line_start:
        return f"{location.file_path}:{location.line_start}-{location.line_end}"
    return f"{location.file_path}:{location.line_start}"


def github_base(source_type: RepositorySourceType, locator: str) -> str | None:
    """``https://github.com/<owner>/<repo>`` for GitHub sources with a well-formed URL."""
    if source_type not in _GITHUB_SOURCES:
        return None
    match = _GITHUB_REPO.match(locator.strip())
    if match is None:
        return None
    return f"https://github.com/{match['owner']}/{match['repo']}"


def github_permalink(
    base: str | None, commit_sha: str | None, location: CodeLocation
) -> str | None:
    """Permalink to the exact lines at the exact commit, never at a moving branch."""
    if base is None or commit_sha is None or not _COMMIT_SHA.match(commit_sha):
        return None
    if location.file_path.startswith("/") or ".." in location.file_path.split("/"):
        return None
    anchor = f"L{location.line_start}"
    if location.line_end > location.line_start:
        anchor += f"-L{location.line_end}"
    return f"{base}/blob/{commit_sha}/{quote(location.file_path)}#{anchor}"


def snippet_lines(snippet: str, first_line: int) -> tuple[list[SnippetLine], bool]:
    """Number a snippet whose first line is ``first_line``; cap at ``MAX_SNIPPET_LINES``."""
    raw = snippet.expandtabs(4).splitlines()
    while raw and not raw[-1].strip():
        raw.pop()
    truncated = len(raw) > MAX_SNIPPET_LINES
    return (
        [SnippetLine(number=first_line + i, text=t) for i, t in enumerate(raw[:MAX_SNIPPET_LINES])],
        truncated,
    )


def diff_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        match = _DIFF_TARGET.match(line)
        if match and match["path"] != "/dev/null" and match["path"] not in files:
            files.append(match["path"])
    return files


def truncate_diff(diff: str) -> tuple[str, bool]:
    lines = diff.splitlines()
    if len(lines) <= MAX_DIFF_LINES:
        return "\n".join(lines), False
    return "\n".join(lines[:MAX_DIFF_LINES]), True


def touches_sensitive_file(paths: list[str]) -> bool:
    return any(is_sensitive_path(p) for p in paths)
