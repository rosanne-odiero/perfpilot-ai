"""AI recommendations & insights tab (served by the AI-advisor API when present)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import data
from frontend.api_client import Unsupported
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import empty_state, not_available
from frontend.components.provenance import provenance_html
from frontend.formatting import items_of
from frontend.theme import badge, esc
from frontend.views.audit_context import AuditContext

HYPOTHESIS_NOTE = "Hypothesis — not measured. Verify with a profile or benchmark before acting."
DETERMINISTIC_NOTE = "Derived deterministically from the static findings (no model involved)."


def _citations(item: dict[str, Any]) -> str:
    cites = item.get("citations") or []
    labels = []
    for c in cites:
        if isinstance(c, dict) and c.get("file_path"):
            start, end = c.get("line_start"), c.get("line_end")
            span = (
                f":{start}" + (f"-{end}" if end and start and end > start else "") if start else ""
            )
            labels.append(f"`{c['file_path']}{span}`")
    return ", ".join(labels)


def _note(provenance: str) -> str:
    return HYPOTHESIS_NOTE if provenance == "AI_HYPOTHESIS" else DETERMINISTIC_NOTE


def _recommendation(item: dict[str, Any]) -> None:
    provenance = str(item.get("provenance") or "STATIC")
    with st.container(border=True):
        rank = item.get("priority_rank")
        title = (
            f"#{rank} · {item.get('title', 'Recommendation')}" if rank else item.get("title", "")
        )
        header = provenance_html(provenance)
        if item.get("effort"):
            header += " " + badge(f"effort {item['effort']}", "#334155")
        if item.get("generated_by"):
            header += " " + badge(str(item["generated_by"]), "#1e293b")
        st.markdown(f"**{esc(title)}**<br>{header}", unsafe_allow_html=True)
        st.caption(_note(provenance))
        if item.get("summary"):
            st.write(item["summary"])
        for label, key in (
            ("Rationale", "rationale"),
            ("Proposed strategy", "proposed_strategy"),
            ("Expected effect (qualitative)", "expected_effect"),
            ("Uncertainty", "uncertainty"),
            ("Correctness risks", "correctness_risks"),
        ):
            if item.get(key):
                st.markdown(f"**{label}:** {item[key]}")
        cites = _citations(item)
        if cites:
            st.markdown(f"**Cited code:** {cites}")
        if item.get("finding_ids"):
            st.caption(f"Based on {len(item['finding_ids'])} finding(s).")
        if item.get("suggested_diff"):
            with st.expander("Suggested diff (unvalidated)"):
                st.code(item["suggested_diff"], language="diff")


def _insight(item: dict[str, Any]) -> None:
    provenance = str(item.get("provenance") or "STATIC")
    with st.container(border=True):
        st.markdown(
            f"**{esc(item.get('title', 'Insight'))}** {provenance_html(provenance)}",
            unsafe_allow_html=True,
        )
        st.caption(_note(provenance))
        if item.get("description"):
            st.write(item["description"])
        if item.get("modules"):
            st.caption("Modules: " + ", ".join(f"`{m}`" for m in item["modules"]))
        cites = _citations(item)
        if cites:
            st.markdown(f"**Cited code:** {cites}")


def _usage(payload: Any) -> None:
    if isinstance(payload, Unsupported) or not isinstance(payload, dict):
        return
    cards = [
        Card("Model", str(payload.get("model") or "none (deterministic)")),
        Card("LLM calls", str(payload.get("calls", 0))),
        Card(
            "Tokens in / out",
            f"{payload.get('input_tokens', 0):,} / {payload.get('output_tokens', 0):,}",
            f"budget {payload.get('budget_tokens', 0):,}",
        ),
        Card("Estimated cost", f"${float(payload.get('estimated_cost_usd') or 0):.4f}"),
        Card(
            "Citations rejected",
            str(payload.get("rejected_citations", 0)),
            f"{payload.get('fallbacks', 0)} fallback(s) to deterministic",
        ),
    ]
    st.markdown("##### LLM usage for this audit")
    render_cards(cards)
    if payload.get("budget_exhausted"):
        st.warning("The token budget was exhausted; later stages used the deterministic advisor.")


def render(context: AuditContext) -> None:
    recommendations = data.audit_optional(context.id, "recommendations")
    if isinstance(recommendations, Unsupported):
        not_available(
            "AI recommendations & insights",
            "Prioritized recommendations, architecture insights and LLM usage appear here once "
            "the API serves /api/audits/{id}/recommendations.",
        )
        return
    st.markdown(
        provenance_html("AI_HYPOTHESIS")
        + " <span class='pp-muted'>AI output is a hypothesis, never a measurement. "
        + provenance_html("STATIC")
        + " recommendations come from the deterministic advisor.</span>",
        unsafe_allow_html=True,
    )
    _usage(data.audit_optional(context.id, "llm-usage"))
    left, right = st.columns([1.5, 1], gap="large")
    with left:
        st.markdown("##### Recommendations")
        recs = items_of(recommendations)
        if not recs:
            empty_state("No recommendations for this audit.")
        for item in sorted(recs, key=lambda r: r.get("priority_rank") or 10**6):
            _recommendation(item)
    with right:
        st.markdown("##### Architecture insights")
        insights = data.audit_optional(context.id, "insights")
        if isinstance(insights, Unsupported):
            st.caption("Architecture insights are not served by this API version.")
        else:
            entries = items_of(insights)
            if not entries:
                st.caption("No architecture insights for this audit.")
            for item in entries:
                _insight(item)
