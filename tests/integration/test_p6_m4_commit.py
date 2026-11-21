"""Physical P1 replay of released M4 results, including failure before publication."""
from dataclasses import replace
from random import Random

import pytest

from src.m2.passengers import FlightInstance, task_id
from src.m3 import M3Searcher
from src.m4 import BaselineOrderRequired
from src.m4.halns import M4Config, M4HALNS
from tests.p4_examples import leg, release, setup


class BFirst(Random):
    def choice(self, items):
        return "B" if "B" in items else items[0]


def solve(sim, plans, **kw):
    snap = sim.batch_snapshot()
    sparse = M3Searcher(sim.router).search(snap, plans)
    result = M4HALNS(sim.router, M4Config(outer_loops_per_request=0)).solve(
        snap, plans, sparse, rng=BFirst(21), previous=sim.search_state, **kw)
    return snap, result


def shared():
    sim = setup([(0, 1, 5), (1, 0, 5)], [("v", 0, 4)])
    plans = {rid: (release(sim, rid, [leg("c", 0, 1, 5, lp=lp)], size=4),)
             for rid, lp in (("A", 420), ("B", 2))}
    return sim, plans


def test_bbaa_to_baa_through_real_commit_and_events(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim, plans = shared()
    snap, result = solve(sim, plans)
    assert result.best_solution.encoding.sequence == tuple("BBAA")
    commit_passengers(sim, snap, plans, result)
    assert sim.batch_snapshot().requests["B"].first_pickup_at == 0
    assert sim.batch_snapshot().requests["A"].accepted_at == 0
    sim.advance_to(2)
    snap, later = solve(sim, {})
    assert sim.search_state.remaining_sequence(snap) == tuple("BAA")
    assert snap.remaining_sequence == tuple("BAA")
    assert later.safe_baseline.completion == {"B": 5, "A": 15}
    assert snap.vehicles["v"].motion.edge.target == 1
    broken = replace(sim.search_state, remaining_task_ids=sim.search_state.remaining_task_ids[:-1])
    with pytest.raises(BaselineOrderRequired) as error:
        M4HALNS(sim.router).solve(snap, {}, M3Searcher(sim.router).search(snap, {}),
                                 previous=broken, rng=Random(0), failure_dir=tmp_path)
    assert error.value.failure_path.is_file()
    commit_passengers(sim, snap, {}, later)
    sim.advance_to(15)
    done = sim.snapshot()
    assert {rid: r.completed_at for rid, r in done.requests.items()} == {"B": 5, "A": 15}
    assert sum(e.object_id == task_id("B", "c", "PICKUP") for e in sim.history) == 1
    assert sim.search_state.remaining_sequence(done) == ()


def test_last_choice_invalid_preserves_queue_and_world(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim, plans = shared()
    snap, result = solve(sim, plans)
    history = sim.history
    with pytest.raises(ValueError) as caught:
        commit_passengers(sim, snap, {"A": plans["A"]}, result, failure_dir=tmp_path)
    assert caught.value.failure_path.exists()
    assert (sim.batch_snapshot(), sim.history, sim.search_state) == (snap, history, None)
    sim.advance_to(15)
    assert all(req.status == "WAITING" for req in sim.snapshot().requests.values())


def test_newly_registered_event_invalidates_in_flight_transaction(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim, plans = shared()
    snap, result = solve(sim, plans)
    sim.schedule_arrival("future", 100, lambda: None)
    with pytest.raises(ValueError, match="stale"):
        commit_passengers(sim, snap, plans, result, failure_dir=tmp_path)
    assert all(r.accepted_at is None for r in sim.snapshot().requests.values())


def test_final_trip_or_load_disagrees_with_executable_stops(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim, plans = shared()
    snap, result = solve(sim, plans)
    out = result.best_solution
    trips = dict(out.trips)
    trips["A"] = (replace(trips["A"][0], pickup=0),)
    row = replace(out.tasks[-1], onboard_after=3)
    rows = out.tasks[:-1] + (row,)
    for invalid in (replace(out, trips=trips), replace(out, vehicles={"v": rows}, tasks=rows)):
        with pytest.raises(ValueError):
            commit_passengers(sim, snap, plans, replace(result, best_solution=invalid), failure_dir=tmp_path)
        assert sim.snapshot() == snap and sim.search_state is None


def test_same_plan_id_cannot_hide_changed_walk_or_time_window(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim, plans = shared()
    snap, result = solve(sim, plans)
    plan = plans["B"][0]
    substitutes = dict(plans, B=(replace(plan, legs=(replace(plan.legs[0], latest_pickup=99),)),))
    with pytest.raises(ValueError):
        commit_passengers(sim, snap, substitutes, result, failure_dir=tmp_path)
    assert sim.snapshot() == snap


def test_stale_snapshot_does_not_touch_existing_passenger_program(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim, plans = shared()
    snap, result = solve(sim, plans)
    commit_passengers(sim, snap, plans, result)
    current, history = sim.batch_snapshot(), sim.history
    with pytest.raises(ValueError, match="stale"):
        commit_passengers(sim, snap, plans, result, failure_dir=tmp_path)
    assert (sim.batch_snapshot(), sim.history) == (current, history)


def test_two_fixed_flights_shared_and_independent_car_pickups_after_landing():
    from src.simulation.transactions import commit_passengers
    sim = setup([(0, 1, 20), (1, 2, 5), (2, 1, 5), (2, 3, 20), (3, 4, 5)],
                [("x", 1, 4), ("y", 3, 4)],
                [FlightInstance("f", 0, 1, 5, 10), FlightInstance("g", 2, 3, 8, 13)])
    plans = {}
    for rid, node in (("A", 0), ("B", 0), ("C", 2)):
        plans[rid] = (release(sim, rid, [leg("air", node, node+1, 5, "FLIGHT"),
                                         leg("car", node+1, node+2, 5)]),)
    snap, result = solve(sim, plans)
    commit_passengers(sim, snap, plans, result)
    booked = sim.snapshot()
    assert booked.flights["f"].reservations == (("A", 1), ("B", 1))
    assert booked.flights["g"].reservations == (("C", 1),)
    assert {req.bindings[-1] for req in booked.requests.values()} == {"x", "y"}
    sim.advance_to(7)
    assert sim.snapshot().requests["B"].status == "ON_FLIGHT"
    later, next_result = solve(sim, {})
    commit_passengers(sim, later, {}, next_result)
    sim.advance_to(100)
    end = sim.snapshot()
    assert all(req.status == "COMPLETED" for req in end.requests.values())
    assert sum(e.object_id == "f" and e.kind == "LAND" for e in sim.history) == 1
    for rid, at in (("A", 10), ("B", 10), ("C", 13)):
        assert end.tasks[task_id(rid, "car", "PICKUP")].completed_at >= at


def test_conflicting_flight_ledger_after_staging_two_parties_rolls_back(tmp_path):
    from src.simulation.transactions import commit_passengers
    sim = setup([(0, 1, 10)], [], [FlightInstance("f", 0, 1, 3, 7)])
    plans = {r: (release(sim, r, [leg("air", 0, 1, 4, "FLIGHT")], size=2),) for r in "AB"}
    snap, result = solve(sim, plans)
    bad = replace(result.best_solution, flight_seats={"f": {"A": 2}})
    with pytest.raises(ValueError, match="seat ledger"):
        commit_passengers(sim, snap, plans, replace(result, best_solution=bad), failure_dir=tmp_path)
    assert sim.batch_snapshot() == snap
    sim.advance_to(7)
    assert [e.kind.value for e in sim.history if e.object_id == "f"] == ["DEPART", "LAND"]
    assert not sim.snapshot().flights["f"].reservations


def test_rebalance_interrupted_after_fractional_current_edge():
    from src.simulation.events import CandidateRoute
    from src.simulation.transactions import commit_passengers
    sim = setup([(0, 1, 5.5), (1, 2, 5), (1, 3, 2)], [("v", 0, 4)])
    plan = release(sim, "R", [leg("c", 1, 3, 2)])
    sim.commit_rebalance(CandidateRoute(sim.batch_snapshot().version, "v", 2))
    sim.advance_to(2)
    plans = {"R": (plan,)}
    snap, result = solve(sim, plans)
    commit_passengers(sim, snap, plans, result)
    assert sim.snapshot().vehicles["v"].motion == snap.vehicles["v"].motion
    sim.advance_to(8)
    assert sim.snapshot().requests["R"].completed_at == 8
    edges = [(e.edge.source, e.edge.target, e.road_purpose)
             for e in sim.drain_history() if e.kind == "EDGE_END"]
    assert edges == [(0, 1, "REBALANCE"), (1, 3, "LOADED")]
    assert sim.drain_history() == ()
