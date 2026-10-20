"""Module dependency graph: node selection, deterministic layout and the Plotly figure (pure)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

import networkx as nx
import plotly.graph_objects as go

from frontend.theme import plotly_template

Layout = Literal["spring", "kamada_kawai"]
LAYOUT_SEED = 7
LABEL_LIMIT = 60
CYCLE_COLOR = "#f43f5e"


@dataclass(frozen=True)
class GraphView:
    figure: go.Figure
    node_names: list[str]
    """Module names in the order of the node trace's points (for selection mapping)."""
    hidden: int


def node_score(node: dict[str, Any]) -> float:
    """Importance used to keep the most relevant modules when the graph is capped."""
    return (
        2.0 * node.get("fan_in", 0)
        + node.get("fan_out", 0)
        + 3.0 * node.get("finding_count", 0)
        + (4.0 if node.get("is_entrypoint") else 0.0)
        + (4.0 if node.get("in_cycle") else 0.0)
    )


def select_nodes(graph: dict[str, Any], top_n: int | None) -> list[str]:
    ranked = sorted(graph.get("nodes", []), key=lambda n: (-node_score(n), n["module_name"]))
    if top_n is not None:
        ranked = ranked[:top_n]
    return sorted(n["module_name"] for n in ranked)


def cycle_edges(graph: dict[str, Any]) -> set[tuple[str, str]]:
    """Edges whose endpoints belong to the same reported import cycle."""
    groups = [set(cycle) for cycle in graph.get("cycles", [])]
    return {
        (e["source"], e["target"])
        for e in graph.get("edges", [])
        if any(e["source"] in g and e["target"] in g for g in groups)
    }


def to_networkx(graph: dict[str, Any], names: list[str]) -> nx.DiGraph:
    keep = set(names)
    digraph = nx.DiGraph()
    digraph.add_nodes_from(sorted(keep))
    digraph.add_edges_from(
        sorted(
            (e["source"], e["target"])
            for e in graph.get("edges", [])
            if e["source"] in keep and e["target"] in keep and e["source"] != e["target"]
        )
    )
    return digraph


def _grid_positions(names: list[str], top: float, width: float) -> dict[str, tuple[float, float]]:
    """Place isolated modules on rows under the connected part instead of letting them drift."""
    per_row = max(1, math.ceil(math.sqrt(len(names) * 3)))
    step = width / max(1, per_row - 1) if per_row > 1 else 0.0
    left = -width / 2
    return {
        name: (left + (i % per_row) * step, top - 0.25 * (1 + i // per_row))
        for i, name in enumerate(sorted(names))
    }


def compute_layout(
    digraph: nx.DiGraph, algorithm: Layout = "spring", seed: int = LAYOUT_SEED
) -> dict[str, tuple[float, float]]:
    """Deterministic positions: connected modules laid out together, isolated ones in a grid."""
    if digraph.number_of_nodes() == 0:
        return {}
    undirected = digraph.to_undirected()
    isolated = [n for n in undirected.nodes if undirected.degree(n) == 0]
    connected = undirected.subgraph(n for n in undirected.nodes if n not in set(isolated))
    positions: dict[str, tuple[float, float]] = {}
    if connected.number_of_nodes() > 0:
        if algorithm == "kamada_kawai" and nx.is_connected(connected):
            raw = nx.kamada_kawai_layout(connected)
        else:
            k = 1.6 / math.sqrt(connected.number_of_nodes())
            raw = nx.spring_layout(connected, seed=seed, k=k, iterations=200)
        positions = {name: (float(x), float(y)) for name, (x, y) in raw.items()}
    if isolated:
        ys = [y for _, y in positions.values()] or [0.0]
        xs = [x for x, _ in positions.values()] or [0.0]
        width = max(1.0, max(xs) - min(xs))
        positions.update(_grid_positions(isolated, min(ys), width))
    return positions


def _hover(node: dict[str, Any]) -> str:
    flags = []
    if node.get("is_entrypoint"):
        flags.append("entrypoint")
    if node.get("in_cycle"):
        flags.append("in import cycle")
    return (
        f"<b>{node['module_name']}</b><br>{node['file_path']}<br>"
        f"imported by {node.get('fan_in', 0)} · imports {node.get('fan_out', 0)}<br>"
        f"findings: {node.get('finding_count', 0)}" + (f"<br>{', '.join(flags)}" if flags else "")
    )


def _edge_trace(
    edges: list[tuple[str, str]],
    pos: dict[str, tuple[float, float]],
    color: str,
    width: float,
    name: str,
) -> go.Scatter:
    xs: list[float | None] = []
    ys: list[float | None] = []
    for source, target in edges:
        (x0, y0), (x1, y1) = pos[source], pos[target]
        xs += [x0, x1, None]
        ys += [y0, y1, None]
    return go.Scatter(
        x=xs, y=ys, mode="lines", line={"color": color, "width": width}, hoverinfo="skip", name=name
    )


def _arrow_trace(
    edges: list[tuple[str, str]], pos: dict[str, tuple[float, float]], color: str
) -> go.Scatter:
    """Small markers at 80% of each edge, pointing at the imported module."""
    xs, ys, angles = [], [], []
    for source, target in edges:
        (x0, y0), (x1, y1) = pos[source], pos[target]
        xs.append(x0 + 0.8 * (x1 - x0))
        ys.append(y0 + 0.8 * (y1 - y0))
        angles.append(90 - math.degrees(math.atan2(y1 - y0, x1 - x0)))
    return go.Scatter(
        x=xs,
        y=ys,
        mode="markers",
        marker={"symbol": "triangle-up", "size": 7, "color": color, "angle": angles},
        hoverinfo="skip",
        showlegend=False,
    )


def build_figure(
    graph: dict[str, Any],
    *,
    top_n: int | None = 40,
    algorithm: Layout = "spring",
    seed: int = LAYOUT_SEED,
    selected: str | None = None,
) -> GraphView:
    names = select_nodes(graph, top_n)
    by_name = {n["module_name"]: n for n in graph.get("nodes", [])}
    digraph = to_networkx(graph, names)
    pos = compute_layout(digraph, algorithm, seed)
    in_cycle = cycle_edges(graph)
    edges = list(digraph.edges)
    normal = [e for e in edges if e not in in_cycle]
    cyclic = [e for e in edges if e in in_cycle]

    nodes = [by_name[name] for name in names]
    findings = [n.get("finding_count", 0) for n in nodes]
    node_trace = go.Scatter(
        x=[pos[n][0] for n in names],
        y=[pos[n][1] for n in names],
        mode="markers+text" if len(names) <= LABEL_LIMIT else "markers",
        text=[n.rsplit(".", 1)[-1] for n in names],
        textposition="top center",
        textfont={"size": 10, "color": "#cbd5e1"},
        hovertext=[_hover(n) for n in nodes],
        hoverinfo="text",
        customdata=[[n["module_name"], n["file_path"]] for n in nodes],
        marker={
            "size": [12 + 7 * math.sqrt(n.get("fan_in", 0)) for n in nodes],
            "color": findings,
            "colorscale": [[0, "#334155"], [0.25, "#6366f1"], [0.6, "#f59e0b"], [1, "#ef4444"]],
            "cmin": 0,
            "cmax": max([1, *findings]),
            "colorbar": {"title": {"text": "findings"}, "thickness": 12},
            "symbol": ["diamond" if n.get("is_entrypoint") else "circle" for n in nodes],
            "line": {
                "width": [
                    3 if n["module_name"] == selected else 2 if n.get("in_cycle") else 1
                    for n in nodes
                ],
                "color": [
                    "#f8fafc"
                    if n["module_name"] == selected
                    else CYCLE_COLOR
                    if n.get("in_cycle")
                    else "#0f172a"
                    for n in nodes
                ],
            },
        },
        name="modules",
        showlegend=False,
    )
    traces = [
        _edge_trace(normal, pos, "rgba(148,163,184,0.35)", 1.0, "imports"),
        _edge_trace(cyclic, pos, CYCLE_COLOR, 2.4, "import cycle"),
    ]
    if len(edges) <= 400:
        traces += [
            _arrow_trace(normal, pos, "rgba(148,163,184,0.6)"),
            _arrow_trace(cyclic, pos, CYCLE_COLOR),
        ]
    figure = go.Figure(data=[*traces, node_trace])
    figure.update_layout(
        template=plotly_template(),
        height=620,
        clickmode="event+select",
        dragmode="pan",
        xaxis={"visible": False},
        yaxis={"visible": False},
        legend={"orientation": "h", "y": -0.02},
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
    )
    return GraphView(figure, names, len(graph.get("nodes", [])) - len(names))


def selected_module(selection: Any, view: GraphView) -> str | None:
    """Module picked in the chart (Streamlit selection state), if any."""
    points = (
        (selection or {}).get("selection", {}).get("points", [])
        if isinstance(selection, dict)
        else []
    )
    node_curve = len(view.figure.data) - 1
    for point in points:
        if point.get("curve_number") == node_curve:
            index = point.get("point_index", point.get("point_number"))
            if isinstance(index, int) and 0 <= index < len(view.node_names):
                return view.node_names[index]
    return None
