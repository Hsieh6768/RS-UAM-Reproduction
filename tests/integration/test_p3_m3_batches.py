from dataclasses import replace
from math import degrees

from src.m1 import M1Planner
from src.m2.passengers import Leg, Plan, Request, task_id
from src.m3 import M3Searcher
from src.simulation.engine import Action, CandidateRoute, Simulator
from tests.p2_examples import network


def test_live_edge_positions_and_cached_m1_refresh_at_each_batch():
    lon = [degrees(x / 6_371_009) for x in (0, 100, 150, 200, 800)]
    net = network(lon, [(0, 3, 40), (3, 4, 100), (1, 4, 120)])
    sim = Simulator(net.road)
    sim.add_vehicle("moving", 0)
    sim.add_vehicle("stationary", 2)
    req = Request("r", 0, 1, 4, 1, 120, pickup_tolerance_s=30)
    sim.schedule_arrival("r", 0, lambda: req)
    before = sim.batch_snapshot()
    planner = M1Planner()
    plans = planner.generate(req, net)
    key = ("r", plans[0].plan_id, plans[0].legs[0].leg_id)
    search = M3Searcher(net.road)
    assert search.search(before, {"r": plans}).by_leg[key] == ("stationary", "moving")
    sim.commit_route(CandidateRoute(before.version, "moving", destination=3))
    sim.advance_to(20)
    in_motion = sim.batch_snapshot()
    assert in_motion.vehicles["moving"].node is None
    assert in_motion.vehicles["moving"].position == net.road.position(1)
    result = search.search(in_motion, {"r": plans})
    assert result.by_leg[key] == ("moving", "stationary")
    assert (result.snapshot_time, result.snapshot_version) == (in_motion.time, in_motion.version)
    assert planner.generate(in_motion.requests["r"], net) is plans
    assert sim.batch_snapshot() == in_motion
    sim.advance_to(30)
    assert search.search(sim.batch_snapshot(), {"r": plans}).by_leg[key] == ()
    sim.advance_to(40)
    after = sim.batch_snapshot()
    assert after.vehicles["moving"].node == 3 and after.vehicles["moving"].motion is None
    assert not search.search(after, {"r": plans}).by_leg


def test_real_commitments_seats_departure_and_landing_across_batches():
    net = network([0, .01], [(0, 1, 100)], [0, 1], [(0, 1, 5)])
    sim = Simulator(net.road)
    for rid, party in [("old", 2), ("small", 2), ("large", 3)]:
        req = Request(rid, 0, 0, 1, party, 100)
        sim.schedule_arrival(rid, 0, lambda req=req: req)
    f = replace(net.air.flight("0>1", 1), depart_at=20, arrive_at=25)
    sim.add_flight(f)
    planner = M1Planner()
    before = sim.batch_snapshot()
    plans = {rid: planner.generate(req, net) for rid, req in before.requests.items()}
    air = {rid: next(p for p in pp if p.service_mode == "RS-UAM") for rid, pp in plans.items()}
    search = M3Searcher(net.road)

    def candidates(rid, result):
        plan = air[rid]
        return result.by_leg[(rid, plan.plan_id, plan.legs[0].leg_id)]

    assert candidates("large", search.search(before, plans)) == (f.flight_id,)
    sim.accept("old", air["old"], (f.flight_id,))
    reserved = sim.batch_snapshot()
    result = search.search(reserved, plans)
    assert candidates("large", result) == () and candidates("small", result) == (f.flight_id,)
    assert result.commitments["old"].bindings == (f.flight_id,)
    assert reserved.flights[f.flight_id].remaining_seats == 2
    assert sim.batch_snapshot() == reserved

    sim.advance_to(20)
    airborne = sim.batch_snapshot()
    result = search.search(airborne, plans)
    assert airborne.flights[f.flight_id].status == "AIRBORNE"
    assert candidates("small", result) == () and result.commitments["old"].plan is air["old"]
    sim.advance_to(30)
    landed = sim.batch_snapshot()
    result = search.search(landed, plans)
    assert landed.requests["old"].status == "COMPLETED" and landed.flights[f.flight_id].status == "LANDED"
    assert not result.commitments and candidates("small", result) == ()


def test_full_vehicle_in_motion_retains_new_candidate_eligibility_before_release():
    net = network([0, .001, .01], [(0, 1, 20), (1, 2, 100)])
    sim = Simulator(net.road)
    sim.add_vehicle("shared", 0)
    old = Request("old", 0, 0, 1, 4, 20)
    new = Request("new", 0, 1, 2, 2, 100)
    sim.schedule_arrival("old", 0, lambda: old)
    sim.schedule_arrival("new", 0, lambda: new)
    plan = Plan("old-plan", "old", (Leg("1", "CAR", 0, 1, 20, 0, 30, 40),))
    sim.accept("old", plan, ("shared",))
    pick = task_id("old", "1", "PICKUP")
    drop = task_id("old", "1", "DROPOFF")
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "shared", actions=(
        Action(0, "PICKUP", pick), Action(0, "DRIVE", 1), Action(20, "DROPOFF", drop))))
    plans = M1Planner().generate(new, net)
    search = M3Searcher(net.road)
    sim.advance_to(10)
    full = sim.batch_snapshot()
    result = search.search(full, {"new": plans})
    assert result.by_leg[("new", plans[0].plan_id, plans[0].legs[0].leg_id)] == ("shared",)
    assert result.commitments["old"].bindings == ("shared",) and result.stats.selected_cars_full == 1
    sim.advance_to(20)
    released = sim.batch_snapshot()
    assert released.vehicles["shared"].occupancy == 0
    after = search.search(released, {"new": plans})
    assert after.by_leg == result.by_leg and after.stats.selected_cars_full == 0


def test_cached_m1_detour_preserves_distinct_pickup_and_dropoff_limits():
    net = network([0, .01, .02], [(0, 1, 10), (1, 2, 110), (0, 2, 100)], (1,))
    req = Request("r", 0, 0, 2, 1, 100)
    sim = Simulator(net.road)
    sim.schedule_arrival("r", 0, lambda: req)
    sim.add_vehicle("at-pickup", 0)
    sim.advance_to(410)
    snap = sim.batch_snapshot()
    plans = M1Planner().generate(req, net)
    detour = next(p for p in plans if len(p.legs) == 2)
    first = detour.legs[0]
    assert (first.earliest_pickup, first.latest_pickup, first.latest_dropoff) == (0, 420, 410)
    result = M3Searcher(net.road).search(snap, {"r": plans})
    assert result.by_leg[("r", detour.plan_id, first.leg_id)] == ("at-pickup",)
