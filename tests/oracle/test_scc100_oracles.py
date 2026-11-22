from copy import deepcopy
from math import nextafter, inf

import numpy as np
import pytest

from src.data import scc100
from src.network.road import RoadNodeIndex, build_graph, summarize_graph


def test_endpoint_limits_are_inclusive_independently():
    above = nextafter(100., inf)
    assert scc100.within_snap_limit(np.array([100, 99, above, 0, above]),
                                    np.array([100, 100, 0, above, above]), 100).tolist() == [
        True, True, False, False, False]


def test_tied_singletons_preserve_smallest_osm_id_without_adding_a_return_edge():
    graph = build_graph({8: (-74, 40.75), 2: (-73.99, 40.75)}, [(7, [8, 2], {"oneway": "yes"})], 6.)
    _, _, largest = summarize_graph(graph)
    assert largest == {2}
    summary, _, _ = summarize_graph(graph.subgraph(largest))
    assert (summary["nodes"], summary["edges"], summary["strong_components"]) == (1, 0, 1)


def test_independent_induced_multigraph_and_nearest_oracle():
    from src.data.scc100_audit import check_induced_graph, nearest_oracle
    # Two SCCs of size two connected by one directed bridge. Nodes 2/3 share coordinates.
    positions = {4: (-73.98, 40.75), 3: (-73.99, 40.75), 2: (-73.99, 40.75), 1: (-74, 40.75)}
    g = build_graph(positions, [(1, [4, 3], {}), (2, [2, 1], {}), (3, [1, 2], {"oneway": "yes"}),
                                (4, [2, 3], {"oneway": "yes"})], 6.)
    _, _, ids = summarize_graph(g)
    assert ids == {1, 2}
    def document(graph):
        return {"nodes": [[n, *p["position"]] for n, p in graph.nodes(data=True)],
                "directed_edges": [[u, v, a["way_id"], a["length_m"], a["time_s"]]
                                   for u, v, a in graph.edges(data=True)]}
    full, selected = document(g), document(g.subgraph(ids))
    assert check_induced_graph(full, selected)["edges"] == 3
    for kind in ("weight", "absent_edge", "duplicate", "wrong_component"):
        broken = deepcopy(selected)
        if kind == "weight":
            broken["directed_edges"][0][-1] += 1
        elif kind == "absent_edge":
            broken["directed_edges"].pop()
        elif kind == "duplicate":
            broken["directed_edges"].append(broken["directed_edges"][0])
        else:
            broken = document(g.subgraph({3, 4}))
        with pytest.raises(ValueError):
            check_induced_graph(full, broken)
    queries = [(-74.0002, 40.7502), (-73.9901, 40.75), positions[3]]
    nodes, distances, _ = RoadNodeIndex(g, g).nearest(queries)
    for q, node, distance in zip(queries, nodes, distances):
        expected, metres = nearest_oracle(full["nodes"], q)
        assert node == expected and distance == pytest.approx(metres, abs=1e-7)

