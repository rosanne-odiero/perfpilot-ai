"""Everything the audit result sections share, loaded once per rerun."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import streamlit as st

from frontend import data
from frontend.api_client import ApiError
from frontend.components.findings_table import SourceContext
from frontend.formatting import github_permalink
from frontend.views.catalog import AuditRow

FINDINGS_MODULE_KEY = "findings_module_filter"
TAB_KEY = "audit_tab"
FILE_FOCUS_KEY = "files_focus"
FILES_TABLE_KEY = "files_table"


def open_in_files(path: str, line_start: int | None = None, line_end: int | None = None) -> None:
    """Widget callback: show ``path`` (with the cited lines highlighted) in the Files tab."""
    st.session_state[FILE_FOCUS_KEY] = {
        "path": path,
        "line_start": line_start,
        "line_end": line_end or line_start,
    }
    st.session_state.pop(FILES_TABLE_KEY, None)
    st.session_state[TAB_KEY] = "Files"


def citation_label(citation: dict[str, Any]) -> str:
    start, end = citation.get("line_start"), citation.get("line_end")
    span = ""
    if start:
        span = f":{start}" + (f"-{end}" if end and end > start else "")
    symbol = f" ({citation['symbol']})" if citation.get("symbol") else ""
    return f"{citation['file_path']}{span}{symbol}"


def render_citations(citations: list[Any], key_prefix: str) -> None:
    """Each cited ``file:line`` as a button that opens the file viewer at those lines."""
    valid = [c for c in citations if isinstance(c, dict) and c.get("file_path")]
    if not valid:
        return
    st.caption("Cited code (opens the file viewer):")
    columns = st.columns(min(3, len(valid)))
    for index, citation in enumerate(valid):
        columns[index % len(columns)].button(
            citation_label(citation),
            key=f"{key_prefix}_cite_{index}",
            icon="📄",
            type="tertiary",
            on_click=open_in_files,
            args=(citation["file_path"], citation.get("line_start"), citation.get("line_end")),
        )


@dataclass(frozen=True)
class AuditContext:
    audit: dict[str, Any]
    row: AuditRow
    snapshot: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.audit["id"])

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self.audit.get("summary") or {})

    def findings(self) -> tuple[list[dict[str, Any]], int]:
        return data.findings(self.id)

    def load_source(self, path: str) -> dict[str, Any] | None:
        try:
            return data.file_content(self.row.snapshot_id, path)
        except ApiError:
            return None

    def permalink(self, finding: dict[str, Any]) -> str | None:
        loc = finding["location"]
        return github_permalink(
            self.row.source_type,
            self.row.source_locator,
            self.row.commit_sha,
            loc["file_path"],
            loc["line_start"],
            loc["line_end"],
        )

    def source_context(self) -> SourceContext:
        return SourceContext(load_source=self.load_source, permalink=self.permalink)
