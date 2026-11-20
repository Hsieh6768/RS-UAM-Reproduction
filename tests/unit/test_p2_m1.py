from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.common.config import load_config
from src.common.timebase import event_second
from src.m1 import M1Config, M1Planner, PlanningConflict
from src.m1.graph import build_graph, earliest_bounds
from src.m1.pulse import TopK
from src.m2.passengers import Leg, Plan, Request, WalkLink
from tests.p2_examples import corridor, network, request


def test_m101_m102_all_directed_connections_and_absolute_bounds():
    net = corridor()
    req = replace(request(net), submitted_at=100)
    graph = build_graph(req, net, M1Config())
    costs = {(e.source, e.target): e.duration for e in graph.edges}
    ground = {(0, 1): 10, (0, 2): 30, (0, 3): 60, (1, 2): 20,
              (1, 3): 50, (2, 1): 5, (2, 3): 30}
    expected = {(("ground", u), ("ground", v)): t for (u, v), t in ground.items()}
    for n in (1, 2, 4):
        g, a = ("ground", n), ("air", f"S{n}")
        expected[g, a] = expected[a, g] = 0
    expected[("air", "S1"), ("air", "S2")] = 5
    assert costs == expected
    bounds = earliest_bounds(graph, req.submitted_at)
    assert bounds == {("ground", 0): 100, ("ground", 1): 110, ("air", "S1"): 110,
                      ("ground", 2): 115, ("air", "S2"): 115, ("ground", 3): 145}


def test_m103_m105_modes_counts_windows_and_ordered_chain():
    net = corridor()
    req = request(net)
    plans = M1Planner().generate(req, net)
    assert [p.nominal_s for p in plans] == [45, 60, 60, 60, 60]
    best = plans[0]
    assert (best.request_id, best.service_mode, best.formal_leg_count, best.transfer_count) == ("R", "RS-UAM", 3, 2)
    assert [s.mode.value for s in best.legs] == ["CAR", "FLIGHT", "CAR"]
    assert best.legs[1].route_id == "1>2"
    assert [(s.earliest_pickup, s.latest_pickup, s.latest_dropoff) for s in best.legs] == [
        (0, 420, 445), (10, 445, 450), (15, 450, 480)]
    a, b, c = (s.leg_id for s in best.legs)
    assert best.step_neighbors(b) == (a, c)
    assert best.step_neighbors(a) == (None, b)
    assert best.step_neighbors(c) == (b, None)
    assert {p.service_mode for p in plans} == {"SHRS", "MHRS", "RS-UAM"}
    for cfg in [M1Config(max_transfers=0), M1Config(max_service_legs=1)]:
        assert [p.nominal_s for p in M1Planner(cfg).generate(req, net)] == [60]
    assert req.status == "WAITING" and req.plan is None


def test_m106_m107_true_worst_and_equal_cost_signatures():
    best = TopK(3)
    for cost in (11, 12, 2, 10):
        best.offer(cost, (str(cost),), cost)
    assert best.values() == (2, 10, 11)
    best = TopK(2)
    best.offer(9, ("z",), "z")
    best.offer(9, ("z",), "z")
    assert best.values() == ("z",) and best.worst_cost is None
    for key in ("c", "a"):
        best.offer(9, (key,), key)
    assert best.values() == ("a", "c")


def test_m108_optional_filter_and_large_valid_gap():
    config = load_config("configs/base.yaml")
    assert M1Config.from_config(config) == M1Config()
    net = network([0, .01, .02], [(0, 1, 600), (1, 2, 600)], (0, 2), [(0, 2, 1)])
    assert [p.nominal_s for p in M1Planner().generate(request(net, 2), net)] == [1, 1200]
    config["m1"]["optional_15min_filter"]["enabled"] = True
    with pytest.raises(NotImplementedError, match="15"):
        M1Config.from_config(config)


def test_m109_empty_or_fewer_than_k():
    net = network([0, .01, .02], [(0, 1, 2)], (1,))
    planner = M1Planner()
    assert planner.generate(Request("none", 0, 0, 2, 1, 9), net) == ()
    assert len(planner.generate(request(net, 1), net)) == 1


def test_m110_cache_eviction_requests_versions_and_configuration():
    net = corridor()
    cfg = M1Config(cache_size=1)
    planner = M1Planner(cfg)
    req = request(net)
    first = planner.generate(req, net)
    with patch.object(net.road, "shortest_time_s", side_effect=AssertionError):
        assert planner.generate(replace(req, ready_at=10), net) is first
    assert planner.generate(replace(req, submitted_at=20), net)[0].legs[0].earliest_pickup == 20
    assert planner.generate(req, net) is not first
    air = replace(net.air, routes={})
    assert planner.generate(req, replace(net, air=air))[0].nominal_s == 60
    changed = network([0, .01, .02, .03], [(0, 3, 99)])
    assert planner.generate(request(changed), changed)[0].nominal_s == 99
    assert len(M1Planner(replace(cfg, top_k=2)).generate(req, net)) == 2


def test_walk_fractional_deadline_and_first_boarding_boundary():
    # Physical road 0->3->1 is .8s in total; the nearby 1->2 link becomes a walk.
    net = network([0, .01, .01001, .006], [(0, 3, ".4"), (3, 1, ".4"),
                  (1, 2, "1.75")], (1,))
    req = request(net, 2, pickup_tolerance_s=0, delay_tolerance_s=0)
    plans = M1Planner(M1Config(walking_speed_mps=1)).generate(req, net)
    best = plans[0]
    assert isinstance(best.legs[1], WalkLink) and best.formal_leg_count == 1
    assert best.legs[0].nominal_s == Decimal(".8")
    assert Decimal("1.11") < best.legs[1].nominal_s < Decimal("1.12")
    assert best.nominal_s < req.final_deadline == Decimal("2.55")
    assert event_second(best.legs[0].nominal_s) + event_second(best.legs[1].nominal_s) == 3
    assert best.legs[0].latest_dropoff + best.legs[1].nominal_s == req.final_deadline

    net = network([0, .001, .02], [(0, 1, 1), (1, 2, 10)], (1,))
    for tolerance, count in [(111, 1), (112, 2)]:
        plans = M1Planner(M1Config(walking_speed_mps=1)).generate(
            request(net, 2, pickup_tolerance_s=tolerance), net)
        assert len(plans) == count
        if count == 2:
            walk, car = next(p.legs for p in plans if len(p.legs) == 2)
            assert car.earliest_pickup == walk.nominal_s
            assert car.latest_pickup == 112


def test_all_walk_counterexample_reproduces_p1_contract():
    with pytest.raises(ValueError, match="formal transport leg"):
        Plan("p", "r", (WalkLink("a", 0, 1, 300, 1), WalkLink("b", 1, 2, 300, 1)))


def test_independent_pickup_and_dropoff_upper_bounds():
    leg = Leg("first", "CAR", 0, 1, 10, 0, 420, 410)
    assert (leg.earliest_pickup, leg.latest_pickup, leg.latest_dropoff) == (0, 420, 410)


@pytest.mark.parametrize("ep,lp,ld", [(11, 10, 20), (11, 20, 10), (11, None, 10), (11, 10, None)])
def test_earliest_pickup_must_respect_each_present_upper_bound(ep, lp, ld):
    with pytest.raises(ValueError, match="Inconsistent leg time window"):
        Leg("first", "CAR", 0, 1, 10, ep, lp, ld)


def test_strict_walk_threshold_and_no_raw_od_refilter():
    net = network([0, .001, .02], [(0, 1, 2), (1, 2, 3)], (1,))
    req = request(net, 2)
    for metres, kind in [("499.999999", "WALK"), ("500", "CAR")]:
        with patch("src.m1.graph.distance_m", return_value=[Decimal(metres)]):
            graph = build_graph(req, net, M1Config())
        assert next(e.kind for e in graph.edges if (e.origin, e.destination) == (0, 1)) == kind
    assert M1Planner().generate(request(net, 1), net)[0].legs[0].mode == "CAR"


def test_distinct_station_ids_cannot_acquire_a_free_interstation_link():
    net = corridor()
    air = replace(net.air, station_nodes=dict(net.air.station_nodes, other=1))
    with pytest.raises(PlanningConflict, match="station"):
        build_graph(request(net), replace(net, air=air), M1Config())
