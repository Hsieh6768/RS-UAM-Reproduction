from dataclasses import replace
from decimal import Decimal
from random import Random
from types import MappingProxyType

import pytest

from src.m2.passengers import FlightInstance, Leg, Plan, task_id
from src.m3 import M3Searcher
from src.m4 import M4Config, M4HALNS
from src.simulation.events import Action, CandidateRoute
from tests.p5_examples import request, world


POINTS = ((0, -73.999, 40.751), (1, -73.997, 40.751),
          (2, -73.982, 40.754), (3, -73.985, 40.752),
          (4, -73.970, 40.754), (5, -73.968, 40.752))


def planner(sim, *, enabled=True):
    from src.m5 import GurobiConfig, M5Config, M5Rebalancer

    return M5Rebalancer(sim.router, M5Config(enabled=enabled, gurobi=GurobiConfig(time_limit_s=2)))


def test_p5_counts_from_post_m4_state_once_by_id():
    sim = world(POINTS, [(0, 1, 5), (1, 0, 5), (1, 2, 7), (2, 3, 2)],
                [("plain", 0), ("pickup", 0), ("loaded", 0), ("fixed", 0), ("off", 0)])
    locked_pickup = request(sim, "pickup-rider", 1, 2)
    loaded = request(sim, "loaded-rider", 0, 1)
    fixed = request(sim, "fixed-rider", 0, 1)
    request(sim, "waiting", 0, 1, size=3)
    request(sim, "old", 0, 1, pickup_tolerance_s=0)
    request(sim, "future", 0, 1, at=10)
    for plan, vid in ((locked_pickup, "pickup"), (loaded, "loaded"), (fixed, "fixed")):
        sim.accept(plan.request_id, plan, (vid,))
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "pickup", 1))
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "loaded", actions=(
        Action(0, "PICKUP", task_id("loaded-rider", "car", "PICKUP")), Action(0, "DRIVE", 1))))
    sim.advance_to(1)
    snapshot = sim.batch_snapshot()
    assert snapshot.requests["old"].status == "EXPIRED_UNSERVED"
    assert "future" not in snapshot.requests
    assert snapshot.vehicles["loaded"].occupancy == 1
    assert not snapshot.vehicles["fixed"].actions
    rebalancer = planner(sim)
    # Multiple M1 alternatives refer back to the same request ID.
    options = tuple(Plan(str(i), "waiting", loaded.legs) for i in range(5))
    plan = rebalancer.propose(
        snapshot, [*(p.request_id for p in options), "old", "future", *("pickup-rider", "fixed-rider")],
        ("plain", "plain", "pickup", "loaded", "fixed"),
    )
    assert plan.status == "READY"
    assert plan.supply == plan.demand == {(0, 0): 1}
    assert plan.transport.flow == {((0, 0), (0, 0)): 1}
    assert plan.local_vehicle_ids == ("plain",)
    assert plan.remaining_vehicle_ids == plan.moves == ()
    assert plan.uncovered_demand == {}
    assert sim.batch_snapshot() == snapshot
    with pytest.raises(TypeError):
        plan.demand[(0, 0)] = 5


def test_p5_still_waiting_but_outside_window_or_future_is_excluded():
    sim = world(POINTS, [(0, 1, 2)], [("v", 0)])
    request(sim, "early", 0, 1, pickup_tolerance_s=2)
    request(sim, "closing", 0, 1, pickup_tolerance_s=3)
    request(sim, "late", 0, 1, at=4)
    sim.advance_to(4)
    live = sim.batch_snapshot()
    snap = replace(live, time=3, requests=MappingProxyType({
        **live.requests, "early": replace(live.requests["early"], status="WAITING"),
        "closing": replace(live.requests["closing"], status="WAITING"),
    }))
    plan = planner(sim).propose(snap, snap.requests, snap.vehicles)
    assert plan.demand == {(0, 0): 1}


@pytest.mark.parametrize(("enabled", "available", "unmet", "reason"), [
    (False, ("v",), ("r",), "DISABLED"),
    (True, (), ("r",), "NO_SUPPLY"),
    (True, ("v",), (), "NO_DEMAND"),
])
def test_p5_skips_gurobi_when_disabled_or_either_side_empty(monkeypatch, enabled, available, unmet, reason):
    import src.m5.rebalancer as module

    sim = world(POINTS, [(2, 3, 1)], [("v", 0)])
    request(sim, "r", 2, 3)
    snap = sim.batch_snapshot()
    monkeypatch.setattr(module, "solve_transport", lambda *_: pytest.fail("Unexpected Gurobi call"))
    plan = planner(sim, enabled=enabled).propose(snap, unmet, available)
    assert (plan.status, plan.reason, plan.transport, plan.moves) == ("SKIPPED", reason, None, ())
    assert plan.remaining_vehicle_ids == available
    assert (snap, ()) == (sim.batch_snapshot(), tuple(e for e in sim.history if e.kind == "EDGE_END"))


def test_p5_dispatch_uses_stable_vehicle_ranking_and_real_shortest_road():
    sim = world(POINTS, [(0, 2, 10), (0, 1, '1.3'), (1, 2, '1.2'), (2, 3, 1)],
                [("c", 0), ("a", 0), ("b", 0)])
    for rid in ("r1", "r2"):
        request(sim, rid, 2, 3)
    snapshot = sim.batch_snapshot()
    rebalancer = planner(sim)
    plan = rebalancer.propose(snapshot, ("r2", "r1"), ("c", "b", "a"))
    replay = rebalancer.propose(snapshot, ("r1", "r2"), ("a", "b", "c"))
    assert plan.status == "READY"
    assert plan.moves == replay.moves
    assert plan.supply_surplus == 1
    assert [move.vehicle_id for move in plan.moves] == ["a", "b"]
    assert plan.remaining_vehicle_ids == ("c",)
    assert plan.covered_demand == {(1, 0): 2} and plan.uncovered_demand == {}
    assert plan.road_distance_m == 40 and plan.road_time_s == 5
    move = plan.moves[0]
    assert move.destination == 2
    assert [(e.source, e.target) for e in move.edges] == [(0, 1), (1, 2)]
    assert move.depart_at == 0 and move.arrive_at == 3 and move.exact_arrival_s == Decimal("2.5")
    sim.commit_rebalance(plan.candidates[0])
    sim.advance_to(3)
    assert sim.batch_snapshot().vehicles["a"].node == 2
    assert sim.batch_snapshot().vehicles["a"].rebalance_target is None
    assert sim.batch_snapshot().requests["r1"].status == "WAITING"
    assert [(e.time, e.physical_time) for e in sim.history if e.kind == "EDGE_END"] == [
        (2, Decimal("1.3")), (3, Decimal("2.5"))]
    with pytest.raises(ValueError, match="stale snapshot"):
        sim.commit_rebalance(plan.candidates[1])


def test_p5_matching_can_reassign_an_earlier_preferred_vehicle_and_find_farther_entry():
    sim = world(POINTS, [(0, 2, 1), (0, 4, 2), (1, 3, 10), (2, 3, 2), (4, 5, 2)],
                [("flex", 0), ("limited", 1)])
    request(sim, "b", 2, 3)
    request(sim, "c", 4, 5)
    snap = sim.batch_snapshot()
    plan = planner(sim).propose(snap, snap.requests, snap.vehicles)
    assert plan.status == "READY"
    assert {m.vehicle_id: m.destination for m in plan.moves} == {"limited": 3, "flex": 4}
    assert plan.transport.flow == {((0, 0), (1, 0)): 1, ((0, 0), (2, 0)): 1}


def test_p5_failure_is_all_or_nothing_and_preserves_car_and_flight_commitments(monkeypatch):
    import src.m5.rebalancer as module
    from src.m5.transport import TransportResult

    sim = world(POINTS, [(0, 2, 1), (2, 3, 2), (4, 5, 1), (5, 4, 1), (4, 0, 2), (1, 0, 1)],
                [("free-a", 0), ("free-b", 0), ("service", 1)])
    flight = FlightInstance("F", 0, 2, 10, 14, route_id="line")
    sim.add_flight(flight)
    request(sim, "history", 1, 2)
    plan = Plan("locked", "history", (Leg("c", "CAR", 1, 0, 1),
                                       Leg("f", "FLIGHT", 0, 2, 4, route_id="line")))
    sim.accept("history", plan, ("service", "F"))
    request(sim, "reachable", 2, 3)
    request(sim, "unreachable", 4, 5)
    snap, history = sim.batch_snapshot(), sim.history
    r = planner(sim)
    original = r.propose(snap, snap.requests, snap.vehicles)
    assert original.status == "UNEXECUTABLE"
    assert original.transport.status == "OPTIMAL" and sum(original.transport.flow.values()) == 2
    assert original.moves == original.candidates == ()
    assert original.remaining_vehicle_ids == ("free-a", "free-b")
    assert original.covered_demand == {} and original.uncovered_demand == original.demand
    failure = original.route_failure
    assert failure.source == (0, 0) and failure.target == (2, 0) and failure.quota == 1
    assert failure.vehicle_ids == ("free-a", "free-b")
    assert failure.entry_nodes == (4, 5) and failure.reachable_options == ()
    assert failure.reason == "NO_DIRECTED_PATH"
    monkeypatch.setattr(module, "solve_transport", lambda *_: TransportResult(
        mode="BALANCED", status="TIME_LIMIT", status_code=9, detail="No proved optimum"))
    failed = r.propose(snap, snap.requests, snap.vehicles)
    assert failed.status == "SOLVER_FAILED" and failed.transport.status == "TIME_LIMIT"
    assert failed.moves == failed.candidates == ()
    assert snap.flights["F"].reserved_seats == 1
    assert (sim.batch_snapshot(), sim.history) == (snap, history)


def test_p5_origin_cell_without_incoming_edges_remains_in_demand():
    sim = world(POINTS, [(2, 4, 1), (0, 4, 2)], [("v", 0)])
    request(sim, "r", 2, 4)
    snap = sim.batch_snapshot()
    plan = planner(sim).propose(snap, snap.requests, snap.vehicles)
    assert (plan.status, plan.reason) == ("UNEXECUTABLE", "NO_ROAD_ENTRY")
    assert plan.route_failure.entry_nodes == ()
    assert plan.route_failure.vehicle_ids == ("v",)
    assert plan.demand == plan.uncovered_demand == {(1, 0): 1}
    assert plan.moves == () and sim.batch_snapshot() == snap


def test_p5_shortage_keeps_realized_coverage_distinct_from_request_acceptance():
    sim = world(POINTS, [(0, 1, 1), (2, 3, 1), (4, 5, 1)], [("v", 0)])
    request(sim, "a", 0, 1)
    request(sim, "b", 2, 3)
    request(sim, "c", 4, 5)
    snap = sim.batch_snapshot()
    plan = planner(sim).propose(snap, snap.requests, snap.vehicles)
    assert plan.status == "READY" and plan.transport.mode == "SHORTAGE"
    assert plan.covered_demand == {(0, 0): 1}
    assert plan.uncovered_demand == {(1, 0): 1, (2, 0): 1}
    assert plan.local_vehicle_ids == ("v",) and not plan.moves
    assert all(req.status == "WAITING" for req in sim.batch_snapshot().requests.values())


def test_p5_active_rebalance_is_eligible_for_real_m4_preemption_after_current_edge():
    sim = world(POINTS, [(0, 1, '5.5'), (1, 2, 8), (1, 3, 2), (2, 4, 1)],
                [("v", 0)])
    request(sim, "original", 2, 4)
    new_request = request(sim, "new", 1, 3, at=2)
    rebalancer = planner(sim)
    snap = sim.batch_snapshot()
    initial = rebalancer.propose(snap, ("original",), ("v",))
    assert initial.moves[0].destination == 2
    sim.commit_rebalance(initial.candidates[0])
    sim.advance_to(2)
    in_motion = sim.batch_snapshot()
    assert in_motion.vehicles["v"].rebalance_target == 2
    assert in_motion.vehicles["v"].motion.edge.target == 1
    assert rebalancer.propose(in_motion, in_motion.requests, ("v",)).reason == "NO_SUPPLY"
    assert planner(sim, enabled=False).propose(in_motion, in_motion.requests, ("v",)).moves == ()
    assert sim.batch_snapshot() == in_motion
    catalog = {"new": (new_request,)}
    sparse = M3Searcher(sim.router).search(in_motion, catalog)
    assert sparse.by_leg["new", new_request.plan_id, "car"] == ("v",)
    result = M4HALNS(sim.router, M4Config(inner_loops=1)).solve(in_motion, catalog, sparse,
                                                               rng=Random(7))
    assert result.best_solution.completion["new"] == 8
    assert (result.config.wall_budget_seconds, result.timing.timed_out) == (10., False)
    sim.accept("new", new_request, ("v",))
    snap = sim.batch_snapshot()
    with pytest.raises(ValueError, match="idle"):
        sim.commit_rebalance(CandidateRoute(snap.version, "v", 2))
    sim.commit_route(CandidateRoute(snap.version, "v", actions=(
        Action(6, "PICKUP", task_id("new", "car", "PICKUP")), Action(6, "DRIVE", 3),
        Action(8, "DROPOFF", task_id("new", "car", "DROPOFF")))))
    redirected = sim.batch_snapshot()
    assert redirected.vehicles["v"].motion == in_motion.vehicles["v"].motion
    assert redirected.vehicles["v"].rebalance_target is None
    sim.advance_to(8)
    assert sim.batch_snapshot().requests["new"].status == "COMPLETED"
    assert [(e.edge.source, e.edge.target, e.time) for e in sim.history if e.kind == "EDGE_END"] == [
        (0, 1, 6), (1, 3, 8)]


def test_p5_publication_validates_route_before_mutation():
    from src.network.routing import UnreachableRoad

    sim = world(POINTS, [(2, 0, 1)], [("v", 0)])
    snap, history = sim.batch_snapshot(), sim.history
    with pytest.raises(UnreachableRoad):
        sim.commit_rebalance(CandidateRoute(snap.version, "v", 2))
    assert (sim.batch_snapshot(), sim.history) == (snap, history)


def test_p5_conflicting_quotas_show_which_vehicle_was_needed_twice():
    sim = world(POINTS, [(0, 2, 1), (0, 4, 2), (2, 3, 2), (4, 5, 2)],
                [("flex", 0), ("isolated", 1)])
    request(sim, "b", 2, 3)
    request(sim, "c", 4, 5)
    snap = sim.batch_snapshot()
    plan = planner(sim).propose(snap, snap.requests, snap.vehicles)
    fault = plan.route_failure
    assert plan.status == "UNEXECUTABLE" and not plan.moves
    assert fault.reason == "INSUFFICIENT_REACHABLE_VEHICLES"
    assert fault.target == (2, 0) and fault.unfilled_slot == 1
    assert [(target, opt.vehicle_id) for target, opt in fault.assigned_options] == [((1, 0), "flex")]
    assert [opt.vehicle_id for opt in fault.reachable_options] == ["flex"]
    assert sim.batch_snapshot() == snap


def test_p5_invalid_optimal_solver_output_fails_with_snapshot(monkeypatch, tmp_path):
    from json import loads

    import src.m5.rebalancer as module
    from src.m5.transport import TransportResult

    sim = world(POINTS, [(0, 2, 1), (2, 3, 1)], [("v", 0)])
    request(sim, "r", 2, 3)
    snap, history = sim.batch_snapshot(), sim.history
    r = planner(sim)
    monkeypatch.setattr(module, "solve_transport", lambda *_: TransportResult(
        mode="BALANCED", status="INVALID_SOLUTION", status_code=2, detail="row residual"))
    with pytest.raises(module.RebalanceInvariantError, match="row residual") as caught:
        r.propose(snap, snap.requests, snap.vehicles, failure_dir=tmp_path)
    record = loads(caught.value.failure_path.read_text(encoding="utf-8"))
    assert record["status"] == "FAILED"
    assert record["transport"]["status_code"] == 2
    assert record["snapshot"]["version"] == snap.version
    assert record["snapshot"]["requests"]["r"]["status"] == "WAITING"
    assert (sim.batch_snapshot(), sim.history) == (snap, history)


def test_p5_m4_and_m5_account_for_time_independently(monkeypatch):
    import src.m5.rebalancer as module

    sim = world(POINTS, [(0, 2, 5), (2, 3, 1)], [("v", 0)])
    request(sim, "unmet", 2, 3)
    snap = sim.batch_snapshot()
    m4 = M4HALNS(sim.router)
    sparse = M3Searcher(sim.router).search(snap, {})
    # Give M4 its own deterministic clock and no service candidate.
    out = m4.solve(snap, {}, sparse, rng=Random(12), wall_clock=lambda: 3., cpu_clock=lambda: 1.)
    clock = iter((10., 20., 21., 26.))
    cpu = iter((1., 2., 3., 4.))
    monkeypatch.setattr(module, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(module, "process_time", lambda: next(cpu))
    plan = planner(sim).propose(snap, snap.requests, snap.vehicles)
    assert plan.status == "READY" and plan.wall_s == 16 and plan.cpu_s == 3
    assert plan.placement_wall_s == plan.placement_cpu_s == 1
    assert out.timing.wall_s == out.timing.cpu_s == 0
    assert out.config.wall_budget_seconds == 10 and not out.timing.timed_out
    assert sim.now == plan.moves[0].depart_at == 0
