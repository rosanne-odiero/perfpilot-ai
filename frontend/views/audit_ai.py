"""AI recommendations & insights tab: advisor output, provenance-labelled, with LLM usage."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import data
from frontend.api_client import Unsupported
from frontend.components.diff_view import render_unified
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import empty_state, not_available
from frontend.components.provenance import provenance_html
from frontend.formatting import humanize, items_of
from frontend.theme import badge, esc
from frontend.views.audit_context import AuditContext, render_citations

HYPOTHESIS_NOTE = "Hypothesis — not measured. Verify with a profile or benchmark before acting."
DETERMINISTIC_NOTE = "Derived deterministically from the static findings (no model involved)."
PROVENANCE_FILTER = {
    "All": None,
    "Deterministic (STATIC)": "STATIC",
    "Model (AI_HYPOTHESIS)": "AI_HYPOTHESIS",
}
STAGE_LABELS = {
    "not_requested": "not requested",
    "no_consent": "no consent given",
    "not_configured": "no LLM configured",
}


def _note(provenance: str) -> str:
    return HYPOTHESIS_NOTE if provenance == "AI_HYPOTHESIS" else DETERMINISTIC_NOTE


def _source_badge(item: dict[str, Any]) -> str:
    """STATIC = deterministic advisor; AI_HYPOTHESIS = model output, named by its model id."""
    provenance = str(item.get("provenance") or "STATIC")
    generated_by = str(item.get("generated_by") or "deterministic")
    label = "deterministic advisor" if generated_by == "deterministic" else f"model {generated_by}"
    return provenance_html(provenance) + " " + badge(label, "#1e293b")


def _recommendation(item: dict[str, Any], index: int) -> None:
    provenance = str(item.get("provenance") or "STATIC")
    with st.container(border=True):
        rank = item.get("priority_rank")
        title = f"#{rank} · {item['title']}" if rank else item["title"]
        header = _source_badge(item)
        if item.get("effort"):
            header += " " + badge(f"effort {item['effort']}", "#334155")
        if item.get("kind"):
            header += " " + badge(str(item["kind"]), "#312e81")
        st.markdown(f"**{esc(title)}**<br>{header}", unsafe_allow_html=True)
        st.caption(_note(provenance))
        if item.get("summary"):
            st.write(item["summary"])
        st.markdown(f"**Rationale.** {item.get('rationale') or '—'}")
        st.markdown(f"**Strategy.** {item.get('proposed_strategy') or '—'}")
        left, right = st.columns(2)
        with left:
            st.markdown("**Expected effect** (qualitative, not measured)")
            st.info(item.get("expected_effect") or "—", icon="🔮")
            st.markdown("**Uncertainty**")
            st.caption(item.get("uncertainty") or "—")
        with right:
            st.markdown("**Correctness risks**")
            st.warning(item.get("correctness_risks") or "None noted.", icon="⚖️")
            if item.get("finding_ids"):
                st.caption(f"Based on {len(item['finding_ids'])} finding(s).")
        render_citations(item.get("citations") or [], f"rec_{index}")
        if item.get("suggested_diff"):
            with st.expander("Suggested diff (unvalidated sketch, never applied)", icon="🩹"):
                render_unified(item["suggested_diff"])


def _insight(item: dict[str, Any], index: int) -> None:
    provenance = str(item.get("provenance") or "STATIC")
    with st.container(border=True):
        st.markdown(
            f"**{esc(item.get('title', 'Insight'))}**<br>{_source_badge(item)}",
            unsafe_allow_html=True,
        )
        st.caption(_note(provenance))
        if item.get("description"):
            st.write(item["description"])
        if item.get("modules"):
            st.caption("Modules: " + ", ".join(f"`{m}`" for m in item["modules"]))
        render_citations(item.get("citations") or [], f"ins_{index}")


def usage_cards(usage: dict[str, Any]) -> list[Card]:
    budget = usage.get("budget_tokens") or 0
    used = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return [
        Card(
            "Model",
            str(usage.get("model") or "none"),
            None if usage.get("model") else "deterministic advisor only",
        ),
        Card("LLM calls", str(usage.get("calls", 0)), f"{usage.get('cache_hits', 0)} cache hits"),
        Card(
            "Tokens in / out",
            f"{usage.get('input_tokens', 0):,} / {usage.get('output_tokens', 0):,}",
            f"budget {budget:,}" + (" · exhausted" if usage.get("budget_exhausted") else "")
            if budget
            else f"{used:,} used",
        ),
        Card("Estimated cost", f"${float(usage.get('estimated_cost_usd') or 0):.4f}"),
        Card(
            "Rejected by guards",
            f"{usage.get('rejected_citations', 0)} citations",
            f"{usage.get('rejected_claims', 0)} numeric claims · "
            f"{usage.get('rejected_items', 0)} items",
        ),
        Card(
            "Fallbacks",
            str(usage.get("fallbacks", 0)),
            f"{usage.get('redactions', 0)} secrets redacted before sending",
        ),
    ]


def _usage(payload: Any) -> None:
    if isinstance(payload, Unsupported) or not isinstance(payload, dict):
        return
    st.markdown("##### LLM usage for this audit")
    render_cards(usage_cards(payload))
    stages = payload.get("stage_status") or {}
    if stages:
        st.caption(
            "Stages: "
            + " · ".join(f"{humanize(k)}: {STAGE_LABELS.get(v, v)}" for k, v in stages.items())
            + (f" · prompts {payload['prompt_version']}" if payload.get("prompt_version") else "")
        )
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
        provenance_html("STATIC")
        + " <span class='pp-muted'>deterministic advisor (no model) · </span>"
        + provenance_html("AI_HYPOTHESIS")
        + " <span class='pp-muted'>model output: a hypothesis, never a measurement. "
        "Expected effects are qualitative.</span>",
        unsafe_allow_html=True,
    )
    _usage(data.audit_optional(context.id, "llm-usage"))
    left, right = st.columns([1.6, 1], gap="large")
    with left:
        head, choice = st.columns([1, 1.4])
        head.markdown("##### Recommendations")
        wanted = PROVENANCE_FILTER[
            choice.segmented_control(
                "Source",
                list(PROVENANCE_FILTER),
                default="All",
                key="ai_rec_filter",
                label_visibility="collapsed",
            )
            or "All"
        ]
        recs = [
            r
            for r in items_of(recommendations)
            if wanted is None or (r.get("provenance") or "STATIC") == wanted
        ]
        if not recs:
            empty_state(
                "No recommendations match."
                if wanted
                else "The advisor produced no recommendations for this audit.",
                "Recommendations are derived from findings; an audit without findings has none.",
            )
        for index, item in enumerate(sorted(recs, key=lambda r: r.get("priority_rank") or 10**6)):
            _recommendation(item, index)
    with right:
        st.markdown("##### Architecture insights")
        insights = data.audit_optional(context.id, "insights")
        if isinstance(insights, Unsupported):
            st.caption("Architecture insights are not served by this API version.")
        else:
            entries = items_of(insights)
            if not entries:
                st.caption("No architecture insights for this audit.")
            for index, item in enumerate(entries):
                _insight(item, index)
