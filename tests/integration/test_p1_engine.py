from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import networkx as nx
import pytest

from src.m2.passengers import FlightInstance, Leg, Plan, Request, WalkLink, task_id
from src.network.routing import RoadRouter, UnreachableRoad
from src.simulation.engine import Action, CandidateRoute, Simulator


def world(edges, **kwargs):
    graph = nx.MultiDiGraph()
    for way, (a, b, duration) in enumerate(edges, start=1):
        for node in (a, b):
            graph.add_node(node, position=(float(node), 0.0))
        graph.add_edge(a, b, way_id=way, time_s=duration, length_m=duration * 6.660896)
    sim = Simulator(RoadRouter(graph), **kwargs)
    sim.add_vehicle("v", 0, capacity=2)
    return sim


def proposal(sim, destination=None, actions=(), vehicle="v"):
    snap = sim.snapshot()
    return CandidateRoute(snap.version, vehicle, destination, tuple(actions))


def timestamps(sim, kind):
    return [(e.time, e.object_id) for e in sim.history if e.kind == kind]


def car_actions(request, leg, pickup, dropoff):
    return (Action(pickup, "PICKUP", task_id(request, leg, "PICKUP")),
            Action(dropoff, "DROPOFF", task_id(request, leg, "DROPOFF")))


def test_core01_03_04_real_clock_arrival_and_hidden_demand():
    sim = world([(0, 1, 17), (1, 2, 7)], start_time=36000)
    reads = []

    def release():
        reads.append(sim.now)
        return Request("new", 36018, 1, 2, 1, 7)

    sim.schedule_arrival("new", 36018, release)
    sim.commit_route(proposal(sim, 2))
    sim.advance_to(36010)
    assert sim.position("v")[0] == pytest.approx(10 / 17)
    assert not reads and "new" not in sim.snapshot().requests
    sim.advance_to(36020)
    assert reads == [36018]
    assert timestamps(sim, "EDGE_END") == [(36017, "v")]
    assert sim.position("v")[0] == pytest.approx(1 + 3 / 7)
    assert sim.snapshot().time == 36020 and "new" in sim.snapshot().requests
    sim.advance_to(36024)
    assert timestamps(sim, "EDGE_END")[-1] == (36024, "v")


def test_core02_07_08_11_15_pickup_wait_capacity_and_candidate_isolation():
    sim = world([(0, 1, 17), (1, 2, 7)])
    req = Request("r", 0, 1, 2, 2, 7)
    sim.schedule_arrival("r", 0, lambda: req)
    leg = Leg("ride", "CAR", 1, 2, 7)
    plan = Plan("p", "r", (leg,))
    sim.accept("r", plan, ("v",))
    pick, drop = car_actions("r", "ride", 18, 25)
    sim.commit_route(proposal(sim, 1, (pick, Action(18, "DRIVE", 2), drop)))
    sim.advance_to(17)
    before = sim.snapshot()
    assert before.vehicles["v"].occupancy == 0
    with pytest.raises((FrozenInstanceError, AttributeError)):
        before.vehicles["v"].node = 2
    with pytest.raises(TypeError):
        before.requests["r"] = req
    sim.advance_to(20)
    assert timestamps(sim, "PICKUP") == [(18, pick.target)]
    assert sim.snapshot().vehicles["v"].occupancy == 2
    assert before.vehicles["v"].occupancy == 0
    carrying = sim.batch_snapshot()
    with pytest.raises(ValueError, match="omits"):
        sim.commit_route(proposal(sim, 2))
    assert sim.snapshot() == carrying
    sim.advance_to(25)
    done = sim.snapshot()
    assert done.requests["r"].completed_at == 25 and done.vehicles["v"].occupancy == 0
    assert done.requests["r"].pickup_deadline == 420
    sim.advance_to(30)
    after = sim.batch_snapshot()
    with pytest.raises(ValueError, match="pending"):
        sim.commit_route(proposal(sim, actions=(replace(pick, time=31),)))
    assert sim.snapshot() == after


def test_core05_06_frozen_edge_replan_atomic_stale_version():
    sim = world([(0, 1, 17), (1, 2, 5), (1, 3, 4), (4, 0, 1)])
    sim.commit_route(proposal(sim, 2, (Action(30, "DRIVE", 2),)))
    sim.advance_to(10)
    old = sim.snapshot()
    assert old.vehicles["v"].motion.edge.target == 1
    candidate = proposal(sim, 3)
    with pytest.raises(UnreachableRoad):
        sim.commit_route(replace(candidate, destination=4))
    assert sim.snapshot() == old
    sim.commit_route(candidate)
    with pytest.raises(ValueError, match="stale"):
        sim.commit_route(candidate)
    assert sim.snapshot().vehicles["v"].motion == old.vehicles["v"].motion
    sim.advance_to(40)
    assert timestamps(sim, "EDGE_END") == [(17, "v"), (21, "v")]
    assert sim.snapshot().vehicles["v"].node == 3
    assert not timestamps(sim, "DRIVE")


def test_core16_fractional_prefixes_position_and_replan():
    sim = world([(0, 1, .4), (1, 2, .4), (2, 3, .4), (3, 4, .4), (3, 5, .75)],
                batch_seconds=1)
    sim.commit_route(proposal(sim, 4))
    sim.advance_to(1)
    assert timestamps(sim, "EDGE_END") == [(1, "v"), (1, "v")]
    assert sim.position("v") == pytest.approx((2.5, 0))
    assert sim.snapshot().vehicles["v"].motion.exact_end == Decimal("1.2")
    sim.commit_route(proposal(sim, 5))
    sim.advance_to(2)
    assert sim.snapshot().vehicles["v"].node == 5
    assert [e.time for e in sim.history if e.kind == "EDGE_END"] == [1, 1, 2, 2]
    assert [e.physical_time for e in sim.history if e.kind == "EDGE_END"] == [
        Decimal(".4"), Decimal(".8"), Decimal("1.2"), Decimal("1.95")]


def test_core12_13_equal_time_ground_air_walk_ground_replay():
    sim = world([(0, 1, 3), (1, 2, 2), (2, 3, 3), (3, 4, 1)])
    sim.add_vehicle("w", 3)
    flight = FlightInstance("f", 1, 2, 3, 8, 2)
    sim.add_flight(flight)
    sim.schedule_arrival("r", 0, lambda: Request("r", 0, 0, 4, 2, 9))
    legs = (Leg("a", "CAR", 0, 1, 3), Leg("b", "FLIGHT", 1, 2, 5),
            WalkLink("walk", 2, 3, 2, 1), Leg("c", "CAR", 3, 4, 1))
    sim.accept("r", Plan("p", "r", legs), ("v", "f", None, "w"))
    pick, drop = car_actions("r", "a", 0, 3)
    sim.commit_route(proposal(sim, actions=(pick, Action(0, "DRIVE", 1), drop)))
    pick, drop = car_actions("r", "c", 10, 11)
    sim.commit_route(proposal(sim, actions=(pick, Action(10, "DRIVE", 4), drop), vehicle="w"))
    sim.advance_to(9)
    snap = sim.snapshot()
    assert all(v.occupancy == 0 for v in snap.vehicles.values())
    assert [e.kind for e in sim.history if e.time == 3] == ["EDGE_END", "DROPOFF", "DEPART"]
    assert [e.kind for e in sim.history if e.time == 8] == ["LAND", "WALK_START"]
    sim.advance_to(12)
    assert sim.snapshot().requests["r"].completed_at == 11
    assert [e.kind for e in sim.history if e.time == 10] == ["WALK_END", "PICKUP", "DRIVE"]


def test_runtime_invariant_failure_freezes_clock_and_writes_diagnostic():
    # The booked passenger is still at the origin when the connecting flight departs.
    failure_dir = Path("outputs") / ("p1-test-" + uuid4().hex)
    sim = world([(0, 1, 2), (1, 2, 2)], failure_dir=failure_dir)
    sim.add_flight(FlightInstance("f", 1, 2, 3, 8, 2))
    sim.schedule_arrival("r", 0, lambda: Request("r", 0, 0, 2, 1, 4))
    plan = Plan("p", "r", (Leg("a", "CAR", 0, 1, 2), Leg("b", "FLIGHT", 1, 2, 5)))
    sim.accept("r", plan, ("v", "f"))
    try:
        with pytest.raises(ValueError):
            sim.advance_to(10)
        assert sim.status == "FAILED" and sim.now == 3
        assert '"FAILED"' in sim.failure_path.read_text(encoding="utf-8")
        with pytest.raises(RuntimeError, match="FAILED"):
            sim.advance_to(20)
    finally:
        if sim.failure_path:
            sim.failure_path.unlink()
            failure_dir.rmdir()


def test_candidate_may_reorder_pending_tasks_but_cannot_break_fixed_connection():
    sim = world([(0, 1, 2), (1, 2, 5)])
    sim.add_flight(FlightInstance("f", 1, 2, 6, 10))
    for rid in ("a", "b"):
        sim.schedule_arrival(rid, 0, lambda r=rid: Request(r, 0, 0, 2, 1, 7))
        sim.accept(rid, Plan("p", rid, (Leg("c", "CAR", 0, 1, 2),
                                      Leg("f", "FLIGHT", 1, 2, 4))), ("v", "f"))
    a, drop_a = car_actions("a", "c", 1, 6)
    b, drop_b = car_actions("b", "c", 2, 6)
    drive = Action(3, "DRIVE", 1)
    sim.commit_route(proposal(sim, actions=(a, b, drive, drop_a, drop_b)))
    before = sim.snapshot()
    with pytest.raises(ValueError):
        sim.commit_route(proposal(sim, actions=(a, b, drive, drop_a, replace(drop_b, time=7))))
    assert sim.snapshot() == before
    sim.commit_route(proposal(sim, actions=(replace(b, time=1), replace(a, time=2), drive, drop_b, drop_a)))
    sim.advance_to(10)
    assert timestamps(sim, "PICKUP") == [(1, b.target), (2, a.target)]
    assert {r.completed_at for r in sim.snapshot().requests.values()} == {10}


def test_snapshot_prediction_from_edge_carry_and_stale_time_only_batch():
    sim = world([(0, 1, 1.25), (1, 2, .75)], batch_seconds=1)
    sim.commit_route(proposal(sim, 2, (Action(8, "DRIVE", 2),)))
    sim.advance_to(1)
    prediction = sim.snapshot().road_arrival("v", 2, sim.router)
    assert prediction == 2
    sim.commit_route(proposal(sim, 2))
    sim.advance_to(2)
    assert timestamps(sim, "EDGE_END")[-1][0] == prediction
    old = proposal(sim)
    sim.advance_to(8)  # only a discarded old-version event at this tick
    with pytest.raises(ValueError, match="stale"):
        sim.commit_route(old)


def test_repeated_seeded_inputs_replay_identically_in_large_and_split_advances():
    from random import Random

    def replay(boundaries):
        sim = world([(0, 1, 1.2), (1, 2, 2)])
        order = ["a", "b", "c"]
        Random(101).shuffle(order)
        for rid in order:
            sim.schedule_arrival(rid, 2, lambda r=rid: Request(r, 2, 0, 2, 1, "3.2"))
        sim.commit_route(proposal(sim, 2))
        for t in boundaries:
            sim.advance_to(t)
        batch = sim.snapshot()
        assert batch.request_types(0) == {rid: 1 for rid in order}
        assert batch.request_types(2) == {rid: 2 for rid in order}
        return sim.history, batch.requests, batch.vehicles

    assert replay([10]) == replay([1, 2, 3, 4, 10])


def test_release_between_batches_can_first_participate_at_next_decision():
    sim = world([(0, 1, 5)])
    sim.schedule_arrival("r", 18, lambda: Request("r", 18, 0, 1, 1, 5))
    plan = Plan("p", "r", (Leg("ride", "CAR", 0, 1, 5),))
    sim.advance_to(18)
    assert sim.snapshot().requests["r"].submitted_at == 18
    with pytest.raises(ValueError, match="batch"):
        sim.accept("r", plan, ("v",))
    sim.advance_to(20)
    assert sim.batch_snapshot().request_types(10)["r"] == 1
    sim.accept("r", plan, ("v",))
    assert sim.batch_snapshot().request_types(10)["r"] == 3


def test_wrong_car_nominal_time_rejected_before_seat_or_plan_commit():
    sim = world([(0, 1, 6)])
    sim.schedule_arrival("r", 0, lambda: Request("r", 0, 0, 1, 1, 6))
    before = sim.snapshot()
    with pytest.raises(ValueError, match="road travel time"):
        sim.accept("r", Plan("p", "r", (Leg("a", "CAR", 0, 1, 1),)), ("v",))
    assert sim.snapshot() == before


def test_final_window_can_close_before_pickup_window():
    sim = world([(0, 1, 1.5)])
    sim.schedule_arrival("r", 0, lambda: Request("r", 0, 0, 1, 1, 1.5, delay_tolerance_s=0))
    sim.advance_to(2)
    assert sim.snapshot().requests["r"].status == "EXPIRED_UNSERVED"


def test_uncaught_runtime_failure_exits_nonzero_with_exact_diagnostic():
    root = Path("outputs") / ("p1-subprocess-" + uuid4().hex)
    script = """
import sys
import networkx as nx
from src.network.routing import RoadRouter
from src.simulation.engine import Simulator
from src.m2.passengers import Request
g = nx.MultiDiGraph()
g.add_node(1, position=(0, 0))
sim = Simulator(RoadRouter(g), failure_dir=sys.argv[1])
sim.schedule_arrival('r', 3, lambda: Request('r', 3, 1, 1, 1, 100))
sim.advance_to(20)
"""
    try:
        run = subprocess.run([sys.executable, "-B", "-c", script, str(root)],
                             capture_output=True, text=True, timeout=20)
        assert run.returncode != 0 and "direct travel time" in run.stderr
        (path,) = root.glob("failure-*.json")
        result = json.loads(path.read_text(encoding="utf-8"))
        assert (result["status"], result["snapshot"]["time"], result["event"]["kind"]) == ("FAILED", 3, "ARRIVAL")
    finally:
        if root.exists():
            for path in root.iterdir():
                path.unlink()
            root.rmdir()


def test_core09_10_remaining_program_is_batch_local_and_runs_without_new_decision():
    sim = world([(0, 1, 17), (1, 2, 4)])
    sim.commit_route(proposal(sim, 2))
    previous = sim.batch_snapshot().remaining_route("v")
    sim.advance_to(10)
    current = sim.batch_snapshot().remaining_route("v")
    assert current.destination == 2 and current.snapshot_version != previous.snapshot_version
    with pytest.raises(ValueError, match="stale"):
        sim.commit_route(previous)
    # With no new decision the physical program continues from the original edge.
    sim.advance_to(30)
    assert timestamps(sim, "EDGE_END") == [(17, "v"), (21, "v")]
    assert sim.snapshot().vehicles["v"].node == 2
