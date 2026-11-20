"""Forward enumeration on a separately constructed physical-road closure.

No production M1 graph, window constructor, signature, or pruning is used here.
"""
from fractions import Fraction as F

import networkx as nx
import pytest

from src.common.geo import distance_m
from src.m1 import M1Config, M1Planner
from src.m2.passengers import WalkLink
from tests.p2_examples import network, request


def enumerate_reference(xs, roads, stations, flights, req, max_legs, max_transfers):
    physical = nx.DiGraph()
    physical.add_nodes_from(range(len(xs)))
    physical.add_weighted_edges_from((u, v, F(t)) for u, v, t in roads)
    layers = nx.DiGraph()
    o, d = ("g", req.origin), ("g", req.destination)
    for u, lengths in nx.all_pairs_bellman_ford_path_length(physical, weight="weight"):
        for v, road_cost in lengths.items():
            if u == v:
                continue
            metres = F(str(distance_m((xs[u], 0), (xs[v], 0))[0]))
            walk = metres < 500 and (u, v) != (req.origin, req.destination)
            signature = ("WALK" if walk else "CAR", u, v, "")
            layers.add_edge(("g", u), ("g", v), item=(signature, metres if walk else road_cost))
    for s in stations:
        layers.add_edge(("g", s), ("a", s), item=None)
        layers.add_edge(("a", s), ("g", s), item=None)
    for u, v, t in flights:
        layers.add_edge(("a", u), ("a", v), item=(("FLIGHT", u, v, f"{u}>{v}"), F(t)))
    answers = {}
    for nodes in nx.all_simple_paths(layers, o, d):
        items = [layers[u][v]["item"] for u, v in zip(nodes, nodes[1:])]
        steps = [x for x in items if x is not None]
        signature = tuple(key for key, t in steps)
        durations = [t for key, t in steps]
        formal = [i for i, (key, t) in enumerate(steps) if key[0] != "WALK"]
        cost = sum(durations)
        if not formal or len(formal) > max_legs or len(formal) - 1 > max_transfers:
            continue
        if req.submitted_at + cost > req.final_deadline:
            continue
        if req.submitted_at + sum(durations[:formal[0]]) > req.pickup_deadline:
            continue
        windows = []
        for i in formal:
            ep = req.submitted_at + sum(durations[:i])
            lp = req.pickup_deadline if i == formal[0] else F(req.final_deadline) - sum(durations[i:])
            ld = F(req.final_deadline) - sum(durations[i + 1:])
            windows.append((ep, lp, ld))
        answers[signature] = cost, signature, tuple(durations), len(formal), len(formal)-1, tuple(windows)
    return sorted(answers.values())


def observed(plan):
    structure, durations, windows = [], [], []
    for step in plan.legs:
        walk = isinstance(step, WalkLink)
        structure.append(("WALK" if walk else step.mode.value, step.origin, step.destination,
                          "" if walk else step.route_id or ""))
        durations.append(F(step.nominal_s))
        if not walk:
            windows.append(tuple(map(F, (step.earliest_pickup, step.latest_pickup, step.latest_dropoff))))
    return (F(plan.nominal_s), tuple(structure), tuple(durations), plan.formal_leg_count,
            plan.transfer_count, tuple(windows))


@pytest.mark.parametrize("tolerance,legs,transfers", [(0, 3, 2), (5, 3, 2), (200, 3, 2),
                                                      (200, 2, 2), (200, 3, 0)])
def test_m104_m105_m111_complete_sets_and_top_k(tolerance, legs, transfers):
    xs = [0, .01, .02, .03, .03001, .05]
    roads = [(0, 1, "10.15"), (1, 2, "11.25"), (2, 3, "12.5"),
             (3, 4, "2.2"), (4, 5, "8.35"), (2, 1, "6"), (4, 2, "8")]
    flights = [(1, 3, "5.5"), (4, 2, "6"), (2, 4, "4.2")]
    stations = (1, 2, 3, 4)
    net = network(xs, roads, stations, flights)
    req = request(net, 5, pickup_tolerance_s=2, delay_tolerance_s=tolerance)
    expected = enumerate_reference(xs, roads, stations, flights, req, legs, transfers)
    assert expected
    for k in (1, 2, 5, len(expected) + 1):
        cfg = M1Config(top_k=k, max_service_legs=legs, max_transfers=transfers, walking_speed_mps=1)
        plans = M1Planner(cfg).generate(req, net)
        assert [observed(p) for p in plans] == expected[:k]
        assert len({p.plan_id for p in plans}) == len(plans)


def test_oracle_checks_leading_intermediate_and_trailing_walk_windows():
    xs = [0, .00001, .02, .02001, .04, .04001]
    roads = [(i, i + 1, 4) for i in range(5)]
    flights = [(1, 2, 1), (3, 4, 1)]
    net = network(xs, roads, (1, 2, 3, 4), flights)
    req = request(net, 5, pickup_tolerance_s=2, delay_tolerance_s=20)
    expected = enumerate_reference(xs, roads, (1, 2, 3, 4), flights, req, 3, 2)
    assert any([s[0] for s in row[1]] == ["WALK", "FLIGHT", "WALK", "FLIGHT", "WALK"]
               for row in expected)
    cfg = M1Config(top_k=len(expected) + 1, walking_speed_mps=1)
    assert [observed(p) for p in M1Planner(cfg).generate(req, net)] == expected


def test_cheapest_path_is_all_walk_and_never_sets_the_top_k_bound():
    xs = [0, .0027, .0054, .02]
    roads = [(0, 1, 500), (1, 2, 500), (0, 3, 350), (3, 2, 750)]
    stations = (1, 3)
    net = network(xs, roads, stations)
    req = request(net, 2)
    expected = enumerate_reference(xs, roads, stations, (), req, 3, 2)
    # Pure walking takes ~600.45 s. Both formal options are slower.
    assert [row[0] for row in expected] == [1000, 1100]
    for k in (1, 2, 5):
        plans = M1Planner(M1Config(top_k=k, walking_speed_mps=1)).generate(req, net)
        assert [observed(p) for p in plans] == expected[:k]


@pytest.mark.parametrize("delay,costs", [(420, [100, 120]), (19, [100])])
def test_independent_windows_and_nominal_detour_eligibility(delay, costs):
    xs, roads = [0, .01, .02], [(0, 1, 10), (1, 2, 110), (0, 2, 100)]
    net = network(xs, roads, (1,))
    req = request(net, 2, delay_tolerance_s=delay)
    expected = enumerate_reference(xs, roads, (1,), (), req, 3, 2)
    assert [row[0] for row in expected] == costs
    assert [observed(p) for p in M1Planner().generate(req, net)] == expected


def test_single_car_with_adjacent_access_and_egress_walks():
    xs = [0, .001, .002, .02, .021, .022]
    roads = [(0, 1, 150), (1, 2, 150), (2, 3, 20), (3, 4, 150), (4, 5, 150)]
    stations = (1, 2, 3, 4)
    net = network(xs, roads, stations)
    req = request(net, 5)
    expected = enumerate_reference(xs, roads, stations, (), req, 1, 0)
    assert any([s[0] for s in row[1]] == ["WALK", "WALK", "CAR", "WALK", "WALK"]
               for row in expected)
    cfg = M1Config(top_k=len(expected) + 1, max_service_legs=1, max_transfers=0, walking_speed_mps=1)
    assert [observed(p) for p in M1Planner(cfg).generate(req, net)] == expected
