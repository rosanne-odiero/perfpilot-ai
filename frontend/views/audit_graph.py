"""Dependency graph tab: module import graph, cycles, module metrics and external imports."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from frontend import data
from frontend.components.dependency_graph import build_figure, selected_module
from frontend.components.notices import empty_state
from frontend.components.provenance import provenance_html
from frontend.theme import plotly_template
from frontend.views.audit_context import FINDINGS_MODULE_KEY, TAB_KEY, AuditContext

DEFAULT_TOP = 40
GRAPH_KEY = "dependency_graph"


def _show_findings_for(path: str) -> None:
    st.session_state[FINDINGS_MODULE_KEY] = path
    st.session_state[TAB_KEY] = "Findings"


def _external_chart(external: dict[str, int], top: int = 15) -> go.Figure:
    items = sorted(external.items(), key=lambda kv: (-kv[1], kv[0]))[:top]
    figure = go.Figure(
        go.Bar(
            x=[n for _, n in items],
            y=[name for name, _ in items],
            orientation="h",
            marker_color="#22d3ee",
            hovertemplate="%{y}: imported by %{x} module(s)<extra></extra>",
        )
    )
    figure.update_layout(
        template=plotly_template(),
        height=max(220, 22 * len(items) + 60),
        title={"text": "Most imported external packages", "font": {"size": 13}},
        yaxis={"autorange": "reversed"},
    )
    return figure


def render(context: AuditContext) -> None:
    graph = data.graph(context.id)
    nodes = graph.get("nodes") or []
    if not nodes:
        empty_state("No Python modules were indexed, so there is no import graph.")
        return
    st.markdown(
        provenance_html("STATIC")
        + " <span class='pp-muted'>Import graph built from the code (no execution). Size = "
        "imported-by count (fan-in) · colour = findings · ◆ = entrypoint · red = import cycle. "
        "Click a module to filter findings.</span>",
        unsafe_allow_html=True,
    )
    controls = st.columns([1, 2, 1.4])
    show_all = controls[0].toggle("Show all modules", value=len(nodes) <= DEFAULT_TOP)
    top_n = None
    if not show_all and len(nodes) > 5:
        top_n = controls[1].slider(
            "Most relevant modules", 5, len(nodes), min(DEFAULT_TOP, len(nodes)), 5
        )
    layout = controls[2].radio(
        "Layout",
        ["spring", "kamada_kawai"],
        horizontal=True,
        format_func=lambda v: v.replace("_", "-"),
    )
    previous = st.session_state.get(FINDINGS_MODULE_KEY)
    by_path = {n["file_path"]: n["module_name"] for n in nodes}
    view = build_figure(graph, top_n=top_n, algorithm=layout, selected=by_path.get(previous or ""))
    if view.hidden:
        st.caption(
            f"Showing the {len(view.node_names)} most relevant of {len(nodes)} modules "
            "(ranked by fan-in, fan-out, findings, entrypoints and cycles)."
        )
    event = st.plotly_chart(
        view.figure,
        theme=None,
        key=GRAPH_KEY,
        on_select="rerun",
        selection_mode="points",
        config={"displaylogo": False, "scrollZoom": True},
    )
    picked = selected_module(event, view)
    if picked:
        node = next(n for n in nodes if n["module_name"] == picked)
        left, right = st.columns([3, 1.2])
        left.info(
            f"**{picked}** · `{node['file_path']}` · imported by {node['fan_in']}, imports "
            f"{node['fan_out']} · {node['finding_count']} finding(s)"
            + (" · entrypoint" if node.get("is_entrypoint") else "")
            + (" · in an import cycle" if node.get("in_cycle") else "")
        )
        right.button(
            "Show findings for this module",
            icon="🔎",
            on_click=_show_findings_for,
            args=(node["file_path"],),
            disabled=node["finding_count"] == 0,
        )
    cycles = graph.get("cycles") or []
    if cycles:
        with st.expander(f"Import cycles ({len(cycles)})", expanded=False):
            for cycle in cycles:
                st.markdown(" → ".join(f"`{m}`" for m in [*cycle, cycle[0]]))
    left, right = st.columns([1.6, 1], gap="large")
    with left:
        st.markdown("##### Modules")
        modules = data.modules(context.id)
        frame = pd.DataFrame(
            [
                {
                    "Module": m["module_name"],
                    "File": m["file_path"],
                    "Lines": m["line_count"],
                    "Fan-in": m["fan_in"],
                    "Fan-out": m["fan_out"],
                    "Findings": m["finding_count"],
                    "Entrypoint": m["is_entrypoint"],
                    "In cycle": m["in_cycle"],
                    "Parse error": m.get("parse_error") or "",
                }
                for m in modules
            ]
        )
        if not frame.empty:
            frame = frame.sort_values(["Findings", "Fan-in"], ascending=False)
        st.dataframe(frame, hide_index=True, width="stretch", height=380)
    with right:
        external = graph.get("external_imports") or {}
        if external:
            st.plotly_chart(_external_chart(external), theme=None)
        else:
            st.caption("No external imports.")
