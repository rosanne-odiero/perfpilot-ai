"""Audit overview: repository summary cards, executive metrics and the top risks."""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from frontend.components.findings_table import location_label, sort_findings
from frontend.components.metric_cards import Card, render_cards
from frontend.components.provenance import provenance_html
from frontend.formatting import SEVERITIES, fmt_dt, human_bytes, humanize, short_sha
from frontend.theme import SEVERITY_COLORS, esc, plotly_template, severity_badge
from frontend.views.audit_context import AuditContext

TOP_RISKS = 6


def _metric_cards(context: AuditContext) -> None:
    counts = context.snapshot.get("counts") or {}
    summary = context.summary
    by_severity = summary.get("by_severity") or {}
    severe = sum(by_severity.get(s, 0) for s in ("CRITICAL", "HIGH"))
    skipped = counts.get("files_skipped", 0)
    cards = [
        Card(
            "Files seen",
            f"{counts.get('files_seen', 0):,}",
            f"{counts.get('files_analyzed', 0):,} kept · {skipped:,} skipped · "
            f"{human_bytes(counts.get('bytes_seen'))}",
        ),
        Card(
            "Python files analyzed",
            f"{summary.get('files_analyzed', 0):,}",
            f"of {counts.get('python_files', 0):,} in inventory · "
            f"{summary.get('parse_errors', 0)} parse errors",
        ),
        Card(
            "Findings",
            f"{summary.get('findings_total', 0):,}",
            f"{severe} critical/high",
            provenance="STATIC",
        ),
        Card(
            "Import graph",
            f"{summary.get('graph_nodes', 0):,} · {summary.get('graph_edges', 0):,}",
            f"modules · imports · {summary.get('cycles', 0)} cycle(s)",
            provenance="STATIC",
        ),
        Card(
            "Entrypoints",
            f"{summary.get('entrypoint_modules', 0):,}",
            f"{summary.get('entrypoint_symbols', 0)} entrypoint symbols",
            provenance="STATIC",
        ),
    ]
    render_cards(cards)


def _severity_chart(by_severity: dict[str, int]) -> go.Figure:
    labels = [s for s in SEVERITIES if by_severity.get(s)]
    figure = go.Figure(
        go.Bar(
            x=[by_severity[s] for s in labels],
            y=labels,
            orientation="h",
            marker_color=[SEVERITY_COLORS[s] for s in labels],
            text=[by_severity[s] for s in labels],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x}<extra></extra>",
        )
    )
    figure.update_layout(
        template=plotly_template(),
        height=230,
        title={"text": "Findings by severity", "font": {"size": 13}},
        yaxis={"autorange": "reversed"},
        xaxis={"title": None, "range": [0, max(by_severity.values()) * 1.18]},
    )
    return figure


def _category_chart(by_category: dict[str, int], top: int = 8) -> go.Figure:
    items = sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    figure = go.Figure(
        go.Bar(
            x=[n for _, n in items],
            y=[humanize(c) for c, _ in items],
            orientation="h",
            marker_color="#8b5cf6",
            text=[n for _, n in items],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x}<extra></extra>",
        )
    )
    figure.update_layout(
        template=plotly_template(),
        height=230,
        title={"text": "Top categories", "font": {"size": 13}},
        yaxis={"autorange": "reversed"},
        xaxis={"range": [0, max(n for _, n in items) * 1.18]},
    )
    return figure


def _top_risks(findings: list[dict[str, Any]]) -> None:
    st.markdown("##### Top risks by priority " + provenance_html("STATIC"), unsafe_allow_html=True)
    if not findings:
        st.caption("No findings: the static rules flagged nothing in the analyzed files.")
        return
    items = "".join(
        f'<div class="pp-list-item">{severity_badge(f["severity"])}'
        f"<div><b>{esc(f['title'])}</b><br><span class='pp-muted'>"
        f"{esc(location_label(f))} · priority {float(f.get('priority_score') or 0):.1f}"
        "</span></div></div>"
        for f in sort_findings(findings)[:TOP_RISKS]
    )
    st.markdown(f'<div class="pp-card">{items}</div>', unsafe_allow_html=True)
    st.caption("Open the Findings tab for code, suggestions and trade-offs.")


def _technologies(technologies: list[dict[str, Any]]) -> None:
    if not technologies:
        st.caption("No languages, frameworks or tools detected.")
        return
    chips = "".join(
        f'<span class="pp-tech" title="{esc(chr(10).join(t.get("evidence") or []))}">'
        f"{esc(t['name'])} <span class='pp-muted'>· {esc(t['kind'])}</span></span>"
        for t in technologies
    )
    st.markdown(chips, unsafe_allow_html=True)
    st.caption("Hover a technology to see the evidence that detected it.")


def _repository_panel(context: AuditContext) -> None:
    snapshot = context.snapshot
    row = context.row
    git = snapshot.get("git") or {}
    facts = {
        "Source": row.source_label,
        "Location": row.source_locator or "—",
        "Commit": row.commit_sha or "— (not a git checkout)",
        "Branch": row.branch or "—",
        "Commit date": fmt_dt(git.get("commit_date")),
        "Uncommitted changes": "—"
        if git.get("is_dirty") is None
        else ("yes" if git["is_dirty"] else "no"),
        "Content hash": short_sha(snapshot.get("content_hash"), 16),
        "Imported": fmt_dt(snapshot.get("created_at")),
    }
    st.dataframe(
        pd.DataFrame({"Field": list(facts), "Value": list(facts.values())}),
        hide_index=True,
        width="stretch",
    )


def _languages(languages: dict[str, int]) -> None:
    if not languages:
        return
    ranked = sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))
    frame = pd.DataFrame(ranked, columns=["Language", "Files"])
    st.dataframe(frame, hide_index=True, width="stretch")


def _dependencies(snapshot: dict[str, Any]) -> None:
    deps = snapshot.get("dependencies") or []
    if deps:
        frame = pd.DataFrame(
            [
                {
                    "Package": d["name"],
                    "Version spec": d.get("version_spec") or "",
                    "Declared in": d["source_file"],
                    "Group": d.get("group") or "",
                }
                for d in deps
            ]
        )
        st.dataframe(frame, hide_index=True, width="stretch")
    else:
        st.caption("No declared dependencies found (pyproject.toml, requirements*.txt, …).")
    if snapshot.get("lock_files"):
        st.caption("Lock files: " + ", ".join(f"`{f}`" for f in snapshot["lock_files"]))


def _limits_and_warnings(context: AuditContext) -> None:
    counts = context.snapshot.get("counts") or {}
    skipped = counts.get("skipped_by_reason") or {}
    limits = list(context.snapshot.get("limits_hit") or []) + list(
        context.summary.get("limits_hit") or []
    )
    warnings = [str(w) for w in context.audit.get("warnings") or []]
    if skipped:
        st.markdown("**Skipped files by reason**")
        st.dataframe(
            pd.DataFrame(
                sorted(skipped.items(), key=lambda kv: (-int(kv[1]), str(kv[0]))),
                columns=["Reason", "Files"],
            ),
            hide_index=True,
            width="stretch",
        )
    for limit in dict.fromkeys(limits):
        st.warning(f"Limit reached: {limit}", icon="📏")
    for warning in warnings:
        st.warning(warning, icon="⚠️")
    errors = context.summary.get("rule_errors", 0)
    if errors:
        st.warning(f"{errors} rule evaluation error(s) were recorded; results may be incomplete.")
    if not (skipped or limits or warnings or errors):
        st.caption("No files skipped, no limits hit, no warnings.")


def render(context: AuditContext) -> None:
    _metric_cards(context)
    findings, _ = context.findings()
    summary = context.summary
    left, right = st.columns([1.25, 1], gap="large")
    with left:
        charts = st.columns(2)
        if summary.get("by_severity"):
            charts[0].plotly_chart(_severity_chart(summary["by_severity"]), theme=None)
        if summary.get("by_category"):
            charts[1].plotly_chart(_category_chart(summary["by_category"]), theme=None)
        _top_risks(findings)
    with right:
        st.markdown("##### Detected technologies")
        _technologies(context.snapshot.get("technologies") or [])
        st.markdown("##### Repository")
        _repository_panel(context)
    lower = st.columns(3, gap="large")
    with lower[0]:
        st.markdown("##### Languages")
        _languages(context.snapshot.get("languages") or {})
    with lower[1]:
        st.markdown("##### Dependencies")
        _dependencies(context.snapshot)
    with lower[2]:
        st.markdown("##### Coverage, limits and warnings")
        _limits_and_warnings(context)
