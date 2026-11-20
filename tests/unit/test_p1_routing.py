from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import NamedTemporaryFile

import networkx as nx
import pytest

from src.common.geo import mph_to_mps
from src.common.timebase import seconds, require_tick, event_second, objective_minutes
from src.network.routing import RoadRouter, UnreachableRoad


def road_graph(edges):
    graph = nx.MultiDiGraph()
    for node in (1, 2, 3, 4):
        graph.add_node(node, position=(-74.0 + node * 0.01, 40.7))
    for a, b, way, length, duration in edges:
        graph.add_edge(a, b, way_id=way, length_m=length, time_s=duration)
    return graph


def test_cross_component_directions_same_node_and_unknown_node():
    # SCC {1,2} can reach SCC {3,4}, but neither downstream node has a return path.
    router = RoadRouter(road_graph([(1, 2, 1, 50, 5), (2, 1, 2, 60, 6),
                                    (2, 3, 3, 20, 2), (3, 4, 4, 30, 3), (4, 3, 5, 40, 4)]))
    assert router.shortest_time_s(1, 4) == Decimal(10)
    assert [e.way_id for e in router.shortest_path_edges(1, 4)] == [1, 3, 4]
    assert router.shortest_time_s(2, 2) == 0
    assert router.shortest_path_edges(2, 2) == ()
    for query in (router.shortest_time_s, router.shortest_path_edges):
        with pytest.raises(UnreachableRoad):
            query(4, 1)
        with pytest.raises(UnreachableRoad):
            query(42, 42)


def test_path_uses_fastest_parallel_edge_and_stable_equal_cost_ties():
    edges = [(1, 2, 8, 1, 9), (1, 2, 7, 2, 0.1), (1, 2, 5, 3, 0.1),
             (2, 4, 6, 8, 0.2), (1, 3, 9, 8, 0.1), (3, 4, 10, 9, 0.2)]
    first, second = (RoadRouter(road_graph(order)) for order in (edges, reversed(edges)))
    assert first.version == second.version
    result = first.shortest_path_edges(1, 4)
    assert result == second.shortest_path_edges(1, 4)
    assert [edge.identity for edge in result] == [(1, 2, 5, 0), (2, 4, 6, 0)]
    assert [(e.length_m, e.time_s) for e in result] == [(3, Decimal("0.1")), (8, Decimal("0.2"))]
    assert first.shortest_time_s(1, 4) == Decimal("0.3")
    with pytest.raises(FrozenInstanceError):
        result[0].time_s = Decimal(99)


def test_graph_owns_immutable_copy_and_versions_weight_changes():
    graph = road_graph([(1, 2, 12, 5, 3)])
    original = RoadRouter(graph)
    graph[1][2][0]["time_s"] = 9
    graph.nodes[1]["position"] = (0, 0)
    updated = RoadRouter(graph)
    assert original.position(1) == (-73.99, 40.7)
    assert original.shortest_time_s(1, 2) == 3
    assert updated.shortest_time_s(1, 2) == 9
    assert original.version != updated.version
    with pytest.raises(AttributeError):
        original.version = "new"


def test_p0_json_edge_precision_survives_reading():
    # Two close alternative costs are distinguishable in decimal JSON.
    with NamedTemporaryFile(dir=".", suffix=".json", delete=False) as fixture:
        path = Path(fixture.name)
        fixture.write(b'{"nodes":[[1,-74,40],[2,-73,40]],"directed_edges":'
                      b'[[1,2,1,8,1.00000000000000002],[1,2,2,9,1.00000000000000001]]}')
    try:
        router = RoadRouter.from_file(path)
    finally:
        path.unlink()
    assert router.shortest_time_s(1, 2) == Decimal("1.00000000000000001")
    assert router.shortest_path_edges(1, 2)[0].way_id == 2


def test_parallel_rows_preserve_slower_and_identical_same_way_occurrences():
    edges = [(1, 2, 5, 5, 3), (1, 2, 5, 1, 2), (1, 2, 5, 1, 2), (1, 2, 4, 1, 9)]
    first, second = (RoadRouter(road_graph(order)) for order in (edges, reversed(edges)))
    parallels = first.parallel_edges(1, 2)
    assert parallels == second.parallel_edges(1, 2)
    assert [edge.identity for edge in parallels] == [(1, 2, 5, 0), (1, 2, 5, 1),
                                                     (1, 2, 5, 2), (1, 2, 4, 0)]
    assert [edge.time_s for edge in parallels] == [2, 2, 3, 9]
    assert first.shortest_path_edges(1, 2) == (parallels[0],)
    assert first.parallel_edges(2, 1) == ()


def test_absolute_arrival_retains_subsecond_remainder_at_low_context_precision():
    from src.common.timebase import exact_add

    with localcontext() as context:
        context.prec = 5
        origin = Decimal("99999999999999999.9")
        whole = exact_add(origin, "0.1")
        assert whole == Decimal("100000000000000000")
        instant = exact_add(whole, Decimal("1e-30"))
        assert instant == Decimal("100000000000000000.000000000000000000000000000001")
        assert event_second(instant) == 100000000000000001


def test_scoring_minutes_are_independent_of_decimal_context():
    with localcontext() as context:
        context.prec = 2
        assert objective_minutes("90.3") == 1.505


def test_equal_parallel_weights_with_different_decimal_spellings_version_stably():
    edges = [(1, 2, 1, 2, Decimal("0.10")), (1, 2, 1, 2, Decimal("0.1"))]
    assert RoadRouter(road_graph(edges)).version == RoadRouter(road_graph(reversed(edges))).version


def test_core16_accumulate_before_ceiling_and_convert_units_at_boundary():
    router = RoadRouter(road_graph([(1, 2, 1, 1, 0.2), (2, 3, 2, 1, 0.2), (3, 4, 3, 1, 0.2)]))
    assert router.shortest_time_s(1, 4) == Decimal("0.6")
    departure = seconds(10)
    physical_time = departure
    ticks = []
    for edge in router.shortest_path_edges(1, 4):
        physical_time += edge.time_s
        ticks.append(event_second(physical_time))
    assert ticks == [11, 11, 11]
    assert event_second(departure + router.shortest_time_s(1, 4)) == 11
    assert event_second(seconds("11.00000000001")) == 12
    assert event_second(11) == require_tick(11) == 11
    assert objective_minutes("90.3") == 1.505
    assert mph_to_mps(14.9) == pytest.approx(6.660896)
    assert mph_to_mps(170) == pytest.approx(75.9968)


@pytest.mark.parametrize("value", [-0.1, "NaN", "Infinity", True, None])
def test_invalid_physical_time_rejected(value):
    with pytest.raises(ValueError):
        seconds(value)


@pytest.mark.parametrize("value", [0.3, -1, True, "4"])
def test_batch_ticks_require_nonnegative_integers(value):
    with pytest.raises(ValueError):
        require_tick(value)
