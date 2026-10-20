"""Findings tab: filterable, prioritized STATIC findings with a detail panel."""

from __future__ import annotations

import streamlit as st

from frontend.components.findings_table import (
    FindingFilters,
    apply_filters,
    filter_options,
    render_detail,
    render_table,
)
from frontend.components.notices import empty_state
from frontend.formatting import humanize
from frontend.views.audit_context import FINDINGS_MODULE_KEY, AuditContext

ALL = "(all)"


def _clear_module() -> None:
    st.session_state.pop(FINDINGS_MODULE_KEY, None)


def _filters(
    options: dict[str, list[str]], max_priority: float, has_measured: bool
) -> FindingFilters:
    row1 = st.columns([1.4, 1.6, 1.2, 1])
    severities = row1[0].multiselect("Severity", options["severities"], key="f_sev")
    categories = row1[1].multiselect(
        "Category", options["categories"], format_func=humanize, key="f_cat"
    )
    rule = row1[2].selectbox("Rule", [ALL, *options["rules"]], key="f_rule")
    provenance = row1[3].selectbox("Provenance", [ALL, *options["provenances"]], key="f_prov")
    row2 = st.columns([2, 1.2, 0.8])
    query = row2[0].text_input("File path contains", key="f_path", placeholder="e.g. shopfast/")
    measured_only = row2[2].toggle(
        "With runtime evidence",
        key="f_measured",
        disabled=not has_measured,
        help="Findings confirmed by a MEASURED runtime hotspot (runtime audits only)",
    )
    min_priority = row2[1].slider(
        "Minimum priority", 0.0, max(1.0, round(max_priority + 0.5, 1)), 0.0, 0.5, key="f_prio"
    )
    module_path = st.session_state.get(FINDINGS_MODULE_KEY)
    if module_path:
        left, right = st.columns([4, 1])
        left.info(f"Filtered to module file `{module_path}` (selected in the dependency graph).")
        right.button("Clear module filter", on_click=_clear_module, key="f_clear_module")
    return FindingFilters(
        severities=frozenset(severities),
        categories=frozenset(categories),
        rule_id=None if rule == ALL else rule,
        provenance=None if provenance == ALL else provenance,
        path_query=query.strip(),
        file_paths=frozenset([module_path]) if module_path else frozenset(),
        min_priority=min_priority,
        measured_only=measured_only,
    )


def render(context: AuditContext) -> None:
    findings, total = context.findings()
    if not findings:
        files = context.summary.get("files_analyzed", 0)
        empty_state(
            f"No findings: the static rules flagged nothing in {files} analyzed Python files.",
            "Check the Files tab for skipped files, or run a runtime audit to measure hotspots.",
        )
        return
    if total > len(findings):
        st.warning(f"Showing the first {len(findings):,} of {total:,} findings.")
    max_priority = max(float(f.get("priority_score") or 0.0) for f in findings)
    has_measured = any(f.get("measured") for f in findings)
    filters = _filters(filter_options(findings), max_priority, has_measured)
    shown = apply_filters(findings, filters)
    st.caption(
        f"{len(shown)} of {len(findings)} findings · sorted by priority · select a row for details"
    )
    if not shown:
        empty_state("No findings match these filters.", "Relax or clear the filters above.")
        return
    selected = render_table(shown, key="findings_table")
    if selected is None:
        st.caption("Select a finding to see its source, suggestion and trade-offs.")
        return
    with st.container(border=True):
        render_detail(selected, context.source_context())
