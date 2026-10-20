from __future__ import annotations

from typing import Any

from frontend.components.dependency_graph import (
    build_figure,
    compute_layout,
    cycle_edges,
    node_score,
    select_nodes,
    selected_module,
    to_networkx,
)


def test_select_nodes_caps_by_relevance(graph: dict[str, Any]) -> None:
    names = select_nodes(graph, top_n=5)
    assert len(names) == 5
    ranked = sorted(graph["nodes"], key=lambda n: (-node_score(n), n["module_name"]))
    assert set(names) == {n["module_name"] for n in ranked[:5]}
    assert len(select_nodes(graph, top_n=None)) == len(graph["nodes"])


def test_cycle_edges_match_reported_cycles(graph: dict[str, Any]) -> None:
    edges = cycle_edges(graph)
    assert edges
    members = set().union(*map(set, graph["cycles"]))
    assert all(a in members and b in members for a, b in edges)


def test_layout_is_deterministic_and_keeps_isolated_nodes_close(graph: dict[str, Any]) -> None:
    names = select_nodes(graph, top_n=None)
    digraph = to_networkx(graph, names)
    first = compute_layout(digraph, "spring", seed=7)
    second = compute_layout(digraph, "spring", seed=7)
    assert first == second
    assert set(first) == set(names)
    xs = [x for x, _ in first.values()]
    ys = [y for _, y in first.values()]
    assert max(xs) - min(xs) < 5
    assert max(ys) - min(ys) < 5
    assert compute_layout(to_networkx(graph, []), "spring") == {}


def test_kamada_kawai_falls_back_on_disconnected_graphs(graph: dict[str, Any]) -> None:
    digraph = to_networkx(graph, select_nodes(graph, top_n=None))
    assert set(compute_layout(digraph, "kamada_kawai")) == set(digraph.nodes)


def test_figure_encodes_size_color_symbols_and_cycles(graph: dict[str, Any]) -> None:
    view = build_figure(graph, top_n=None)
    nodes = view.figure.data[-1]
    by_name = {n["module_name"]: n for n in graph["nodes"]}
    assert [nodes.customdata[i][0] for i in range(len(view.node_names))] == view.node_names
    assert list(nodes.marker.color) == [by_name[n]["finding_count"] for n in view.node_names]
    symbols = dict(zip(view.node_names, nodes.marker.symbol, strict=True))
    assert all(
        (symbols[n] == "diamond") == bool(by_name[n]["is_entrypoint"]) for n in view.node_names
    )
    cycle_trace = view.figure.data[1]
    assert cycle_trace.name == "import cycle"
    assert any(x is not None for x in cycle_trace.x)
    assert view.hidden == 0
    capped = build_figure(graph, top_n=4)
    assert capped.hidden == len(graph["nodes"]) - 4


def test_selected_module_maps_points_of_the_node_trace(graph: dict[str, Any]) -> None:
    view = build_figure(graph, top_n=None)
    node_curve = len(view.figure.data) - 1
    pick = {"selection": {"points": [{"curve_number": node_curve, "point_index": 2}]}}
    assert selected_module(pick, view) == view.node_names[2]
    edge_click = {"selection": {"points": [{"curve_number": 0, "point_index": 2}]}}
    assert selected_module(edge_click, view) is None
    assert selected_module(None, view) is None
    assert selected_module({"selection": {"points": [{"curve_number": node_curve}]}}, view) is None
