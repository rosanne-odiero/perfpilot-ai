"""Files tab: browse the inventory and read source with finding lines highlighted."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import data
from frontend.api_client import ApiError
from frontend.components.code_view import render_code
from frontend.components.file_explorer import (
    SENSITIVE,
    FileFilters,
    files_frame,
    filter_files,
    finding_lines,
    folders,
)
from frontend.components.findings_table import location_label, sort_findings
from frontend.components.notices import show_api_error
from frontend.formatting import human_bytes
from frontend.theme import esc, severity_badge, severity_cell_style
from frontend.views.audit_context import AuditContext

WITHHELD = "Content withheld: this file matches a sensitive-file pattern and is never served."


def _filters(frame_languages: list[str], all_folders: list[str]) -> FileFilters:
    cols = st.columns([2, 1.4, 1.4, 1, 1])
    query = cols[0].text_input("Search path", placeholder="e.g. orders.py", key="files_query")
    folder = cols[1].selectbox("Folder", ["(all)", *all_folders], key="files_folder")
    languages = cols[2].multiselect("Language", frame_languages, key="files_lang")
    only = cols[3].toggle("With findings", key="files_only_findings")
    skipped = cols[4].toggle("Show skipped", value=True, key="files_show_skipped")
    return FileFilters(
        query=query.strip(),
        languages=tuple(languages),
        folder=None if folder == "(all)" else folder,
        only_with_findings=only,
        include_skipped=skipped,
    )


def _source(context: AuditContext, row: dict[Any, Any], findings: list[dict[str, Any]]) -> None:
    path = str(row["Path"])
    status = str(row["Status"])
    if status.startswith("skipped"):
        reason = status.split(": ", 1)[-1]
        if reason == SENSITIVE:
            st.warning(WITHHELD, icon="🔒")
        else:
            st.info(f"Not analyzed and not stored ({reason}).", icon="🚫")
        return
    try:
        content = data.file_content(context.row.snapshot_id, path)
    except ApiError as exc:
        if exc.code == "sensitive_file":
            st.warning(WITHHELD, icon="🔒")
        else:
            show_api_error(exc, f"Reading {path}")
        return
    in_file = sort_findings(f for f in findings if f["location"]["file_path"] == path)
    note = (
        f"{len(in_file)} finding(s) highlighted" if in_file else human_bytes(content["size_bytes"])
    )
    if content.get("redactions"):
        st.caption(f"🔐 {content['redactions']} secret-like value(s) replaced with [REDACTED].")
    render_code(
        content["content"],
        language=content.get("language"),
        filename=path,
        highlight=finding_lines(findings, path),
        title=path,
        note=note,
    )
    if in_file:
        items = "".join(
            f'<div class="pp-list-item">{severity_badge(f["severity"])}<div>'
            f"<b>{esc(f['title'])}</b><br><span class='pp-muted'>{esc(location_label(f))} · "
            f"{esc(f['rule_id'])}</span></div></div>"
            for f in in_file
        )
        st.markdown(f'<div class="pp-card">{items}</div>', unsafe_allow_html=True)


def render(context: AuditContext) -> None:
    files = data.snapshot_files(context.row.snapshot_id)
    counts = data.file_finding_counts(context.id)
    findings, _ = context.findings()
    frame = files_frame(files, counts)
    if frame.empty:
        st.info("The inventory of this snapshot is empty.")
        return
    filters = _filters(sorted(set(frame["Language"]) - {""}), folders(frame["Path"].tolist()))
    shown = filter_files(frame, filters)
    st.caption(
        f"{len(shown)} of {len(frame)} files · select a row to view its source. "
        "Counts and severities are STATIC findings."
    )
    max_findings = max(1, int(frame["Findings"].max()))
    state = st.dataframe(
        shown.drop(columns=["Folder"]).style.map(severity_cell_style, subset=["Max severity"]),
        key="files_table",
        hide_index=True,
        width="stretch",
        height=360,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Path": st.column_config.TextColumn(width="large"),
            "Size": st.column_config.NumberColumn(format="%d B"),
            "Findings": st.column_config.ProgressColumn(
                format="%d", min_value=0, max_value=max_findings
            ),
            "Max priority": st.column_config.NumberColumn(format="%.1f"),
        },
    )
    rows = state.selection.rows if state is not None else []
    if rows and rows[0] < len(shown):
        _source(context, shown.iloc[rows[0]].to_dict(), findings)
    else:
        st.caption("No file selected.")
