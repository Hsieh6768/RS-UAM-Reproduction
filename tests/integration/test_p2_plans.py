"""P2/P1 boundary checks using only artificial road graphs."""
from dataclasses import replace
from decimal import Decimal

import pytest

from src.common.timebase import exact_add
from src.m1 import M1Config, M1Planner
from src.m2.passengers import Leg, Plan, WalkLink, task_id
from src.simulation.engine import Action, CandidateRoute, Simulator
from src.simulation.service import ServiceConflict, ServiceState
from tests.p2_examples import network, request


def test_planning_and_acceptance_are_separate_at_fractional_deadline():
    net = network([0, .01, .01001, .006], [(0, 3, ".4"), (3, 1, ".4"),
                  (1, 2, "1.75")], (1,))
    req = request(net, 2, pickup_tolerance_s=0, delay_tolerance_s=0)
    service = ServiceState()
    service.add_request(req)
    plan = M1Planner(M1Config(walking_speed_mps=1)).generate(req, net)[0]
    assert service.requests["R"].status == "WAITING"
    assert not service.tasks and not service.flights
    assert isinstance(plan.legs[1], WalkLink) and plan.nominal_s < req.final_deadline
    with pytest.raises(ServiceConflict, match="Final deadline"):
        service.accept("R", plan, ("car", None), now=0)
    assert service.requests["R"].status == "WAITING" and not service.tasks


def test_two_adjacent_walks_after_boarding_take_time_and_no_extra_legs():
    net = network([0, .01, .011, .012], [(0, 1, 20), (1, 2, 150), (2, 3, 150)], (1, 2))
    req = request(net, pickup_tolerance_s=0)
    plans = M1Planner(M1Config(max_service_legs=1, max_transfers=0)).generate(req, net)
    plan = next(p for p in plans if len(p.legs) == 3)
    car, one, two = plan.legs
    assert isinstance(one, WalkLink) and isinstance(two, WalkLink)
    assert plan.step_neighbors(one.leg_id) == (car.leg_id, two.leg_id)
    assert plan.formal_leg_count == 1 and plan.transfer_count == 0
    assert plan.nominal_s == exact_add(car.nominal_s, exact_add(one.nominal_s, two.nominal_s))
    service = ServiceState()
    service.add_request(req)
    service.accept("R", plan, ("car", None, None), now=0)
    assert service.requests["R"].first_pickup_at is None
    assert all(task.started_at is None for task in service.tasks.values())


def test_all_walk_candidate_leaves_only_direct_car():
    net = network([0, .0027, .0054], [(0, 1, 50), (1, 2, 50)], (1,))
    req = request(net, 2)
    only, = M1Planner().generate(req, net)
    assert [(s.mode, s.origin, s.destination, s.nominal_s) for s in only.legs] == [("CAR", 0, 2, 100)]
    assert req.status == "WAITING" and req.plan is None


def test_nominally_feasible_detour_preserves_d002_first_window():
    net = network([0, .01, .02], [(0, 1, 10), (1, 2, 110), (0, 2, 100)], (1,))
    direct, detour = M1Planner().generate(request(net, 2), net)
    assert (direct.nominal_s, detour.nominal_s) == (100, 120)
    assert [(s.earliest_pickup, s.latest_pickup, s.latest_dropoff) for s in detour.legs] == [
        (0, 420, 410), (10, 410, 520)]


def test_route_commit_and_execution_keep_the_local_dropoff_deadline():
    net = network([0, .01], [(0, 1, 10)])
    req = request(net, 1)
    leg = Leg("car", "CAR", 0, 1, 10, 0, 420, 410)
    plan = Plan("p", req.request_id, (leg,))
    sim = Simulator(net.road)
    sim.add_vehicle("v", 0)
    sim.schedule_arrival("R", 0, lambda: req)
    sim.accept("R", plan, ("v",))
    before = sim.snapshot()
    pick, drop = (task_id("R", "car", k) for k in ("PICKUP", "DROPOFF"))

    def route(at):
        return CandidateRoute(before.version, "v", None, (Action(at, "PICKUP", pick),
                              Action(at, "DRIVE", 1), Action(at + 10, "DROPOFF", drop)))

    with pytest.raises(ServiceConflict, match="Latest dropoff"):
        sim.commit_route(route(405))
    assert sim.snapshot() == before
    sim.commit_route(route(400))
    sim.advance_to(410)
    done = sim.snapshot().requests["R"]
    assert (done.first_pickup_at, done.completed_at, done.status) == (400, 410, "COMPLETED")

    # Exercise the independent event-time guard even if a bad dropoff reaches execution.
    service = ServiceState()
    service.add_request(req)
    service.accept("R", plan, ("v",), now=0)
    service.execute("PICKUP", pick, 405, lambda _: 0, lambda _: 4)
    with pytest.raises(ServiceConflict, match="Latest dropoff"):
        service.execute("DROPOFF", drop, 415, lambda _: 1, lambda _: 4)
    assert service.requests["R"].status == "ON_VEHICLE" and service.occupancy("v") == 1


def test_exact_last_instant_and_first_later_decimal():
    # The last road leg is 1.000...0001s, well below an event-second resolution.
    for tail, count in [("1", 2), ("1.00000000000000000000000000000000001", 1)]:
        net = network([0, .01, .02], [(0, 1, 2), (1, 2, tail), (0, 2, 3)], (1,))
        plans = M1Planner().generate(request(net, 2, pickup_tolerance_s=0, delay_tolerance_s=0), net)
        assert len(plans) == count and all(p.nominal_s == Decimal(3) for p in plans)


def test_changed_road_rejects_stale_direct_time_and_changed_cache_settings_apply():
    a = network([0, .01, .02], [(0, 1, 10), (1, 2, 10)], (1,))
    b = network([0, .01, .02], [(0, 1, 20), (1, 2, 20)], (1,))
    planner = M1Planner()
    req = request(a, 2)
    assert len(planner.generate(req, a)) == 2
    with pytest.raises(ValueError, match="direct"):
        planner.generate(req, b)
    planner.config = replace(planner.config, top_k=1)
    assert len(planner.generate(req, a)) == 1
