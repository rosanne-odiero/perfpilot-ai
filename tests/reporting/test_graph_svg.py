from __future__ import annotations

import xml.etree.ElementTree as ET

from backend.models.audit_schemas import DependencyGraph, GraphEdge, GraphNode
from backend.reporting.graph_svg import (
    finding_color,
    node_radius,
    render_dependency_graph,
    select_nodes,
)

SVG_NS = "{http://www.w3.org/2000/svg}"


def _graph(n: int, *, cycle: bool = True) -> DependencyGraph:
    nodes = [
        GraphNode(
            module_name=f"pkg.mod{i:03d}",
            file_path=f"pkg/mod{i:03d}.py",
            fan_in=(n - i) % 7,
            finding_count=i % 4,
            is_entrypoint=i == 0,
            in_cycle=cycle and i in (1, 2),
        )
        for i in range(n)
    ]
    edges = [
        GraphEdge(source=f"pkg.mod{i:03d}", target=f"pkg.mod{(i * 3 + 1) % n:03d}")
        for i in range(n)
    ]
    if cycle:
        edges += [
            GraphEdge(source="pkg.mod001", target="pkg.mod002"),
            GraphEdge(source="pkg.mod002", target="pkg.mod001"),
        ]
    return DependencyGraph(
        nodes=nodes, edges=edges, cycles=[["pkg.mod001", "pkg.mod002"]] if cycle else []
    )


def test_rendering_is_deterministic_and_valid_svg() -> None:
    first = render_dependency_graph(_graph(15))
    second = render_dependency_graph(_graph(15))
    assert first is not None
    assert second is not None
    assert first.svg == second.svg
    root = ET.fromstring(first.svg)
    assert root.tag == f"{SVG_NS}svg"
    circles = [c for c in root.iter(f"{SVG_NS}circle") if c.find(f"{SVG_NS}title") is not None]
    assert len(circles) == 15
    assert first.note is None
    assert "http" not in first.svg.replace("http://www.w3.org/2000/svg", "")


def test_entrypoints_cycles_and_finding_colors_are_encoded() -> None:
    rendering = render_dependency_graph(_graph(10))
    assert rendering is not None
    root = ET.fromstring(rendering.svg)
    by_title = {
        c.find(f"{SVG_NS}title").text.split(" ")[0]: c  # type: ignore[union-attr]
        for c in root.iter(f"{SVG_NS}circle")
        if c.find(f"{SVG_NS}title") is not None
    }
    assert by_title["pkg.mod000"].get("stroke") == "#1d4ed8"  # entrypoint outline
    assert by_title["pkg.mod001"].get("stroke") == "#b91c1c"  # in a cycle
    assert by_title["pkg.mod003"].get("fill") == finding_color(3)
    red_lines = [line for line in root.iter(f"{SVG_NS}line") if line.get("stroke") == "#b91c1c"]
    assert len(red_lines) >= 2  # both directions of the cycle (+ legend sample)


def test_large_graphs_are_capped_to_most_relevant_nodes() -> None:
    graph = _graph(120)
    rendering = render_dependency_graph(graph, max_nodes=30)
    assert rendering is not None
    assert rendering.shown_nodes == 30
    assert rendering.total_nodes == 120
    assert rendering.note == "Showing 30 of 120 modules"
    assert "Showing 30 of 120 modules" in rendering.svg
    shown = {n.module_name for n in select_nodes(graph.nodes, 30)}
    assert {"pkg.mod000", "pkg.mod001", "pkg.mod002"} <= shown  # entrypoint + cycle members


def test_labels_are_escaped_and_empty_graph_renders_nothing() -> None:
    graph = DependencyGraph(
        nodes=[GraphNode(module_name="evil.<script>x</script>", file_path="evil.py")]
    )
    rendering = render_dependency_graph(graph)
    assert rendering is not None
    assert "<script>" not in rendering.svg
    ET.fromstring(rendering.svg)
    assert render_dependency_graph(DependencyGraph()) is None


def test_size_and_color_scales() -> None:
    assert node_radius(0) == 6.0
    assert node_radius(4) == 12.0
    assert node_radius(10_000) == 22.0
    assert finding_color(0) != finding_color(1) != finding_color(3) != finding_color(6)
    assert finding_color(50) == finding_color(6)
