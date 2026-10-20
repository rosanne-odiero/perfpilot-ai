"""Module import graph (networkx): edges, fan-in/fan-out, cycles and reachability."""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Iterable

import networkx as nx

from backend.models.audit_schemas import DependencyGraph, GraphEdge, GraphNode, ModuleInfo

MAX_CYCLES = 50
MAX_CYCLE_LENGTH = 8


def build_import_graph(modules: list[ModuleInfo]) -> nx.DiGraph:
    graph = nx.DiGraph()
    for module in sorted(modules, key=lambda m: m.module_name):
        graph.add_node(module.module_name, file_path=module.file_path)
    for module in sorted(modules, key=lambda m: m.module_name):
        for ref in module.imports:
            target = ref.resolved_module
            if not ref.is_internal or not target or target == module.module_name:
                continue
            if graph.has_edge(module.module_name, target):
                data = graph.edges[module.module_name, target]
                data["line"] = min(data["line"], ref.line)
            else:
                graph.add_edge(module.module_name, target, line=ref.line)
    return graph


def _normalize_cycle(cycle: list[str]) -> list[str]:
    pivot = cycle.index(min(cycle))
    return cycle[pivot:] + cycle[:pivot]


def find_cycles(graph: nx.DiGraph) -> tuple[list[list[str]], set[str]]:
    """Up to ``MAX_CYCLES`` simple cycles (length <= ``MAX_CYCLE_LENGTH``) plus every module that
    sits in any cycle (exact, from strongly connected components)."""
    in_cycle: set[str] = set()
    for component in nx.strongly_connected_components(graph):
        if len(component) > 1:
            in_cycle |= component
    if not in_cycle:
        return [], in_cycle
    cyclic = graph.subgraph(sorted(in_cycle))
    raw = itertools.islice(nx.simple_cycles(cyclic, length_bound=MAX_CYCLE_LENGTH), MAX_CYCLES)
    cycles = sorted({tuple(_normalize_cycle(list(c))) for c in raw})
    return [list(c) for c in cycles], in_cycle


def external_imports(modules: Iterable[ModuleInfo]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for module in modules:
        for ref in module.imports:
            if not ref.is_internal and ref.level == 0 and ref.module:
                counts[ref.module.split(".", 1)[0]] += 1
    return dict(sorted(counts.items()))


def to_dependency_graph(graph: nx.DiGraph, modules: list[ModuleInfo]) -> DependencyGraph:
    cycles, in_cycle = find_cycles(graph)
    by_name = {m.module_name: m for m in modules}
    nodes = [
        GraphNode(
            module_name=name,
            file_path=by_name[name].file_path,
            fan_in=graph.in_degree(name),
            fan_out=graph.out_degree(name),
            is_entrypoint=by_name[name].is_entrypoint,
            in_cycle=name in in_cycle,
        )
        for name in sorted(graph.nodes)
    ]
    edges = [
        GraphEdge(source=s, target=t, line=data.get("line"))
        for s, t, data in sorted(graph.edges(data=True), key=lambda e: (e[0], e[1]))
    ]
    return DependencyGraph(
        nodes=nodes, edges=edges, external_imports=external_imports(modules), cycles=cycles
    )


def reachable_modules(graph: nx.DiGraph, entry_modules: Iterable[str]) -> set[str]:
    reached: set[str] = set()
    for entry in entry_modules:
        if entry in graph and entry not in reached:
            reached.add(entry)
            reached |= nx.descendants(graph, entry)
    return reached
