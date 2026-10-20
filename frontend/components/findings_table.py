"""Findings: client-side filters (pure), the results table and the detail panel."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import streamlit as st

from frontend.components.code_view import context_window, render_code
from frontend.components.metric_cards import Card, render_cards
from frontend.components.provenance import provenance_html
from frontend.formatting import SEVERITY_RANK, fmt_ms, fmt_pct, humanize
from frontend.theme import badge, esc, severity_badge, severity_cell_style


@dataclass(frozen=True)
class FindingFilters:
    severities: frozenset[str] = frozenset()
    categories: frozenset[str] = frozenset()
    rule_id: str | None = None
    provenance: str | None = None
    path_query: str = ""
    file_paths: frozenset[str] = field(default_factory=frozenset)
    min_priority: float = 0.0

    @property
    def active(self) -> bool:
        return self != FindingFilters()


def location_label(finding: dict[str, Any]) -> str:
    loc = finding["location"]
    if loc["line_end"] > loc["line_start"]:
        return f"{loc['file_path']}:{loc['line_start']}-{loc['line_end']}"
    return f"{loc['file_path']}:{loc['line_start']}"


def matches(finding: dict[str, Any], filters: FindingFilters) -> bool:
    path = finding["location"]["file_path"]
    return (
        (not filters.severities or finding["severity"] in filters.severities)
        and (not filters.categories or finding["category"] in filters.categories)
        and (filters.rule_id is None or finding["rule_id"] == filters.rule_id)
        and (filters.provenance is None or finding.get("provenance") == filters.provenance)
        and (not filters.path_query or filters.path_query.lower() in path.lower())
        and (not filters.file_paths or path in filters.file_paths)
        and float(finding.get("priority_score") or 0.0) >= filters.min_priority
    )


def sort_findings(findings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Highest priority first; severity, then location break ties deterministically."""
    return sorted(
        findings,
        key=lambda f: (
            -float(f.get("priority_score") or 0.0),
            SEVERITY_RANK.get(f["severity"], 99),
            f["location"]["file_path"],
            f["location"]["line_start"],
        ),
    )


def apply_filters(
    findings: Iterable[dict[str, Any]], filters: FindingFilters
) -> list[dict[str, Any]]:
    return sort_findings(f for f in findings if matches(f, filters))


def filter_options(findings: list[dict[str, Any]]) -> dict[str, list[str]]:
    severities = {f["severity"] for f in findings}
    return {
        "severities": sorted(severities, key=lambda s: SEVERITY_RANK.get(s, 99)),
        "categories": sorted({f["category"] for f in findings}),
        "rules": sorted({f["rule_id"] for f in findings}),
        "provenances": sorted({f.get("provenance") or "STATIC" for f in findings}),
    }


def findings_frame(findings: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Priority": float(f.get("priority_score") or 0.0),
                "Severity": f["severity"],
                "Finding": f["title"],
                "Location": location_label(f),
                "Rule": f["rule_id"],
                "Category": humanize(f["category"]),
                "Confidence": f["confidence"],
                "Provenance": f.get("provenance") or "STATIC",
            }
            for f in findings
        ],
        columns=[
            "Priority",
            "Severity",
            "Finding",
            "Location",
            "Rule",
            "Category",
            "Confidence",
            "Provenance",
        ],
    )


def render_table(
    findings: list[dict[str, Any]], key: str, height: int = 420
) -> dict[str, Any] | None:
    """Show the table; return the finding the user selected (if any)."""
    frame = findings_frame(findings)
    max_priority = max([100.0, *frame["Priority"].tolist()]) if not frame.empty else 100.0
    state = st.dataframe(
        frame.style.map(severity_cell_style, subset=["Severity"]),
        key=key,
        hide_index=True,
        width="stretch",
        height=height,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Priority": st.column_config.ProgressColumn(
                "Priority",
                help="severity x confidence x path criticality (see docs/static-analysis.md)",
                format="%.1f",
                min_value=0.0,
                max_value=max_priority,
            ),
            "Finding": st.column_config.TextColumn(width="large"),
            "Location": st.column_config.TextColumn(width="medium"),
        },
    )
    rows = state.selection.rows if state is not None else []
    return findings[rows[0]] if rows and rows[0] < len(findings) else None


@dataclass(frozen=True)
class SourceContext:
    """What the detail panel needs to show source and permalinks for a finding."""

    load_source: Callable[[str], dict[str, Any] | None]
    permalink: Callable[[dict[str, Any]], str | None]


def _factors_frame(factors: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Factor": humanize(k), "Value": round(float(v), 4)} for k, v in factors.items()]
    )


def render_detail(finding: dict[str, Any], context: SourceContext) -> None:
    loc = finding["location"]
    header = " ".join(
        [
            severity_badge(finding["severity"]),
            provenance_html(finding.get("provenance")),
            badge(f"confidence {finding['confidence']}", "#334155"),
            badge(finding["rule_id"], "#1e293b"),
            badge(humanize(finding["category"]), "#1e293b"),
        ]
    )
    st.markdown(f"#### {esc(finding['title'])}")
    st.markdown(header, unsafe_allow_html=True)
    symbol = f" · in `{loc['symbol']}`" if loc.get("symbol") else ""
    link = context.permalink(finding)
    link_md = f" · [View on GitHub at this commit ↗]({link})" if link else ""
    st.markdown(f"`{location_label(finding)}`{symbol}{link_md}")
    st.write(finding["description"])

    source = context.load_source(loc["file_path"])
    highlight = range(loc["line_start"], loc["line_end"] + 1)
    if source is not None and source.get("content") is not None:
        total = max(1, source["content"].count("\n") + 1)
        render_code(
            source["content"],
            language=source.get("language"),
            filename=loc["file_path"],
            highlight=highlight,
            window=context_window(loc["line_start"], loc["line_end"], total),
            title=loc["file_path"],
            note=f"lines {loc['line_start']}-{loc['line_end']} highlighted",
        )
    else:
        st.caption("Source unavailable for this file; showing the snippet stored with the finding.")
        render_code(finding["snippet"], language="python", title="snippet")

    left, right = st.columns(2)
    with left:
        st.markdown("**Suggestion**")
        st.success(finding["suggestion"], icon="💡")
        if finding.get("complexity_note"):
            st.markdown("**Complexity (static estimate)**")
            st.caption(finding["complexity_note"])
    with right:
        st.markdown("**Trade-offs**")
        st.warning(finding["tradeoffs"], icon="⚖️")
        st.markdown(f"**Priority score {float(finding.get('priority_score') or 0):.1f}**")
        if finding.get("priority_factors"):
            st.dataframe(_factors_frame(finding["priority_factors"]), hide_index=True)

    measured = finding.get("measured")
    if measured:
        profile_id = esc(measured.get("profile_id", ""))
        function = esc(measured.get("function_name", ""))
        st.markdown(
            provenance_html("MEASURED")
            + f" **Runtime evidence from profile `{profile_id}`** · `{function}`",
            unsafe_allow_html=True,
        )
        render_cards(
            [
                Card("Exclusive time", fmt_ms(measured.get("internal_time_ms")), None, "MEASURED"),
                Card(
                    "Cumulative time", fmt_ms(measured.get("cumulative_time_ms")), None, "MEASURED"
                ),
                Card("Calls", str(measured.get("call_count", "—")), None, "MEASURED"),
                Card(
                    "Share of profiled time",
                    fmt_pct(measured.get("percentage_of_profiled_time")),
                    None,
                    "MEASURED",
                ),
            ]
        )
        if measured.get("note"):
            st.caption(measured["note"])
