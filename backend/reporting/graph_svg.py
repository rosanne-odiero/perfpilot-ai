"""Deterministic static SVG rendering of the module dependency graph.

No network access and no external renderer: positions come from a seeded networkx spring layout
over a stable node order, and coordinates are rounded, so the same graph always yields the same
SVG. Large graphs are capped to the most relevant modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from html import escape

import networkx as nx

from backend.models.audit_schemas import DependencyGraph, GraphNode

LAYOUT_SEED = 7
DEFAULT_MAX_NODES = 40

_WIDTH = 920
_HEIGHT = 620
_MARGIN = 60
_LEGEND_HEIGHT = 46

_FINDING_COLORS: list[tuple[int, str, str]] = [
    (0, "#cbd5e1", "no findings"),
    (1, "#fde68a", "1-2 findings"),
    (3, "#fb923c", "3-5 findings"),
    (6, "#dc2626", "6+ findings"),
]
_ENTRY_STROKE = "#1d4ed8"
_CYCLE_COLOR = "#b91c1c"
_EDGE_COLOR = "#94a3b8"


@dataclass(frozen=True)
class GraphRendering:
    svg: str
    shown_nodes: int
    total_nodes: int
    note: str | None


def finding_color(count: int) -> str:
    color = _FINDING_COLORS[0][1]
    for threshold, value, _ in _FINDING_COLORS:
        if count >= threshold:
            color = value
    return color


def node_radius(fan_in: int) -> float:
    return min(22.0, 6.0 + 3.0 * math.sqrt(max(0, fan_in)))


def relevance(node: GraphNode) -> float:
    """Ranking used to pick which modules to draw when the graph is capped."""
    return (
        node.fan_in * 2.0
        + node.fan_out
        + node.finding_count * 3.0
        + (5.0 if node.is_entrypoint else 0.0)
        + (4.0 if node.in_cycle else 0.0)
    )


def select_nodes(nodes: list[GraphNode], max_nodes: int) -> list[GraphNode]:
    ranked = sorted(nodes, key=lambda n: (-relevance(n), n.module_name))
    return sorted(ranked[:max_nodes], key=lambda n: n.module_name)


def _short_label(module_name: str, limit: int = 28) -> str:
    parts = module_name.split(".")
    label = ".".join(parts[-2:]) if len(parts) > 1 else module_name
    return label if len(label) <= limit else "…" + label[-(limit - 1) :]


def _cycle_edges(cycles: list[list[str]]) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for cycle in cycles:
        for i, source in enumerate(cycle):
            edges.add((source, cycle[(i + 1) % len(cycle)]))
    return edges


def _spring(
    names: list[str], edges: list[tuple[str, str]], box: tuple[float, float, float, float]
) -> dict[str, tuple[float, float]]:
    """Seeded spring layout of ``names`` scaled into ``box`` = (left, top, width, height)."""
    left, top, width, height = box
    if len(names) == 1:
        return {names[0]: (round(left + width / 2, 1), round(top + height / 2, 1))}
    graph = nx.Graph()
    graph.add_nodes_from(names)
    graph.add_edges_from(edges)
    raw = nx.spring_layout(graph, seed=LAYOUT_SEED, k=2.0 / math.sqrt(len(names)), iterations=150)
    xs = [float(raw[n][0]) for n in names]
    ys = [float(raw[n][1]) for n in names]
    span_x = (max(xs) - min(xs)) or 1.0
    span_y = (max(ys) - min(ys)) or 1.0
    return {
        n: (
            round(left + (float(raw[n][0]) - min(xs)) / span_x * width, 1),
            round(top + (float(raw[n][1]) - min(ys)) / span_y * height, 1),
        )
        for n in names
    }


def _layout(names: list[str], edges: list[tuple[str, str]]) -> dict[str, tuple[float, float]]:
    """Connected modules get a spring layout; modules without shown edges go in a bottom row."""
    linked = sorted({n for edge in edges for n in edge})
    isolated = [n for n in names if n not in set(linked)]
    bottom = _HEIGHT - _LEGEND_HEIGHT - _MARGIN
    row_y = bottom
    if isolated and linked:
        bottom -= 70
    positions: dict[str, tuple[float, float]] = {}
    if linked:
        positions.update(
            _spring(linked, edges, (_MARGIN, _MARGIN, _WIDTH - 2 * _MARGIN, bottom - _MARGIN))
        )
    if isolated:
        step = (_WIDTH - 2 * _MARGIN) / len(isolated)
        y = row_y if linked else (_HEIGHT - _LEGEND_HEIGHT) / 2
        for i, name in enumerate(isolated):
            positions[name] = (round(_MARGIN + step * (i + 0.5), 1), round(y, 1))
    return positions


def _arrow(
    start: tuple[float, float],
    end: tuple[float, float],
    target_radius: float,
    offset: float = 0.0,
) -> tuple[str, str] | None:
    """Return (line endpoints, arrowhead polygon) stopping at the target circle's edge.

    ``offset`` shifts the edge sideways so the two directions of a reciprocal import stay
    visible."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < target_radius + 4:
        return None
    ux, uy = dx / length, dy / length
    start = (start[0] - uy * offset, start[1] + ux * offset)
    end = (end[0] - uy * offset, end[1] + ux * offset)
    tip_x, tip_y = end[0] - ux * (target_radius + 1), end[1] - uy * (target_radius + 1)
    base_x, base_y = tip_x - ux * 8, tip_y - uy * 8
    left = (base_x - uy * 3.5, base_y + ux * 3.5)
    right = (base_x + uy * 3.5, base_y - ux * 3.5)
    line = f'x1="{start[0]:.1f}" y1="{start[1]:.1f}" x2="{base_x:.1f}" y2="{base_y:.1f}"'
    head = f"{tip_x:.1f},{tip_y:.1f} {left[0]:.1f},{left[1]:.1f} {right[0]:.1f},{right[1]:.1f}"
    return line, head


def _legend(y: float) -> list[str]:
    parts = ['<g font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#334155">']
    x = 16.0
    for _, color, label in _FINDING_COLORS:
        parts.append(f'<circle cx="{x + 6:.1f}" cy="{y:.1f}" r="6" fill="{color}"/>')
        parts.append(f'<text x="{x + 16:.1f}" y="{y + 4:.1f}">{escape(label)}</text>')
        x += 104
    parts.append(
        f'<circle cx="{x + 6:.1f}" cy="{y:.1f}" r="6" fill="#ffffff" '
        f'stroke="{_ENTRY_STROKE}" stroke-width="3"/>'
    )
    parts.append(f'<text x="{x + 16:.1f}" y="{y + 4:.1f}">entrypoint</text>')
    x += 92
    parts.append(
        f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + 22:.1f}" y2="{y:.1f}" '
        f'stroke="{_CYCLE_COLOR}" stroke-width="2.5"/>'
    )
    parts.append(f'<text x="{x + 28:.1f}" y="{y + 4:.1f}">import cycle</text>')
    x += 112
    parts.append(f'<text x="{x:.1f}" y="{y + 4:.1f}">node size = fan-in</text>')
    parts.append("</g>")
    return parts


def render_dependency_graph(
    graph: DependencyGraph, *, max_nodes: int = DEFAULT_MAX_NODES
) -> GraphRendering | None:
    """Render ``graph`` as a standalone SVG string, or ``None`` when it has no nodes."""
    if not graph.nodes:
        return None
    selected = select_nodes(graph.nodes, max_nodes)
    names = [n.module_name for n in selected]
    name_set = set(names)
    edges = sorted(
        {
            (e.source, e.target)
            for e in graph.edges
            if e.source in name_set and e.target in name_set and e.source != e.target
        }
    )
    positions = _layout(names, edges)
    cycle_edges = _cycle_edges(graph.cycles)
    by_name = {n.module_name: n for n in selected}
    total = len(graph.nodes)
    note = f"Showing {len(selected)} of {total} modules" if len(selected) < total else None

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
        f'viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" aria-label="Module dependency graph">',
        "<title>Module dependency graph</title>",
        f'<rect x="0" y="0" width="{_WIDTH}" height="{_HEIGHT}" fill="#ffffff"/>',
    ]
    edge_set = set(edges)
    for source, target in edges:
        in_cycle = (source, target) in cycle_edges
        color = _CYCLE_COLOR if in_cycle else _EDGE_COLOR
        width = 2.2 if in_cycle else 1.0
        arrow = _arrow(
            positions[source],
            positions[target],
            node_radius(by_name[target].fan_in),
            offset=3.0 if (target, source) in edge_set else 0.0,
        )
        if arrow is None:
            continue
        line, head = arrow
        out.append(f'<line {line} stroke="{color}" stroke-width="{width}"/>')
        out.append(f'<polygon points="{head}" fill="{color}"/>')
    for node in selected:
        x, y = positions[node.module_name]
        radius = node_radius(node.fan_in)
        if node.is_entrypoint:
            stroke, stroke_width = _ENTRY_STROKE, 3.0
        elif node.in_cycle:
            stroke, stroke_width = _CYCLE_COLOR, 2.0
        else:
            stroke, stroke_width = "#475569", 0.8
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" '
            f'fill="{finding_color(node.finding_count)}" stroke="{stroke}" '
            f'stroke-width="{stroke_width}"><title>{escape(node.module_name)} '
            f"(fan-in {node.fan_in}, findings {node.finding_count})</title></circle>"
        )
        out.append(
            f'<text x="{x:.1f}" y="{y + radius + 11:.1f}" text-anchor="middle" '
            f'font-family="Helvetica, Arial, sans-serif" font-size="10" fill="#0f172a">'
            f"{escape(_short_label(node.module_name))}</text>"
        )
    legend_y = _HEIGHT - _LEGEND_HEIGHT / 2
    out.extend(_legend(legend_y))
    if note:
        out.append(
            f'<text x="{_WIDTH - 16}" y="20" text-anchor="end" '
            f'font-family="Helvetica, Arial, sans-serif" font-size="11" fill="#475569">'
            f"{escape(note)}</text>"
        )
    out.append("</svg>")
    return GraphRendering(svg="".join(out), shown_nodes=len(selected), total_nodes=total, note=note)
