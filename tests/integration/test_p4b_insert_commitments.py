"""Table 3 insertion in real fragments with carried passenger commitments."""
from dataclasses import replace
from random import Random

import pytest

from src.m2.passengers import Plan, Request
from src.m3 import M3Searcher
from src.m4 import Encoding, FullDecoder
from src.m4.halns import M4Config, M4HALNS
from src.m4.operators import apply_operator
from src.m4.partitioned_decoder import PartitionedDecoder
from src.m4.task_graph import TaskGraph
from src.m4.validation import ScheduleValidator
from src.simulation.transactions import commit_passengers
from tests.p4_examples import leg, setup


class CommitOrder(Random):
    def __init__(self, sequence):
        super().__init__(1)
        self.sequence = iter(sequence)

    def choice(self, options):
        return "V" if "V" in options else next(self.sequence)


class InsertPositions:
    def __init__(self, source, destination):
        self.source = source
        self.destination = destination

    def choice(self, positions):
        self.eligible = tuple(positions)
        return self.eligible[self.source]

    def randrange(self, stop):
        self.slot_count = stop
        assert 0 <= self.destination < stop
        return self.destination


def corridor():
    edges = [(a, b, 10) for i in range(3) for a, b in ((i, i + 1), (i + 1, i))]
    return setup(edges, [("V", 0, 4)])


def add_party(sim, rid, submitted_at, origin, destination, pickup_by, dropoff_by, size=2):
    travel = (destination - origin) * 10
    req = Request(rid, submitted_at, origin, destination, size, travel,
                  pickup_tolerance_s=pickup_by - submitted_at,
                  delay_tolerance_s=dropoff_by - submitted_at - travel)
    sim.schedule_arrival(rid, submitted_at, lambda: req)
    step = leg("car", origin, destination, travel, ep=submitted_at, lp=pickup_by, ld=dropoff_by)
    return (Plan("p-" + rid, rid, (step,)),)


def solve_initial(sim, plans, rng):
    snap = sim.batch_snapshot()
    sparse = M3Searcher(sim.router).search(snap, plans)
    result = M4HALNS(sim.router, M4Config(outer_loops_per_request=0)).solve(
        snap, plans, sparse, rng=rng, previous=sim.search_state
    )
    return snap, result


def commit_initial(sim, plans, sequence):
    snap, result = solve_initial(sim, plans, CommitOrder(sequence))
    assert result.best_solution.encoding.sequence == tuple(sequence)
    commit_passengers(sim, snap, plans, result)
    sim.advance_to(1)


def insert(full, encoding, source, destination):
    fragments = tuple(comp.positions for comp in TaskGraph.build(full, encoding).components)
    positions = tuple(range(len(encoding.sequence)))
    assert fragments == (positions,)
    rng = InsertPositions(source, destination)
    targets = []
    trial = apply_operator("insert", full, encoding, rng, fragments=fragments, on_move=targets.append)
    assert rng.eligible == positions and rng.slot_count == len(positions)
    assert trial.choices == encoding.choices
    return trial, targets


def stops(schedule):
    return [(row.task.request_id, row.task.kind, row.at, row.onboard_after)
            for row in schedule.tasks]


@pytest.mark.parametrize("a_deadline", [45, 25])
def test_insert_moves_type3_dropoff_in_all_type3_component(a_deadline):
    sim = corridor()
    plans = {"A": add_party(sim, "A", 0, 0, 2, 5, a_deadline),
             "C": add_party(sim, "C", 0, 0, 3, 5, 45)}
    commit_initial(sim, plans, "ACAC")
    snap, baseline = solve_initial(sim, {}, Random(1))
    before_history, before_state = sim.history, sim.search_state
    vehicle = snap.vehicles["V"]
    assert snap.request_types() == {"A": 3, "C": 3}
    assert set(vehicle.onboard) == {"A", "C"}
    assert (vehicle.motion.edge.source, vehicle.motion.edge.target, vehicle.motion.exact_end) == (0, 1, 10)
    full = FullDecoder(sim.router, snap, {}, M3Searcher(sim.router).search(snap, {}))
    source = full.safe_baseline.encoding
    assert source.sequence == tuple("AC")

    trial, targets = insert(full, source, source=1, destination=0)
    assert trial.sequence == tuple("CA")
    assert targets == [("C",)]
    decoded = full.decode(trial)
    assert decoded == PartitionedDecoder(full).decode(trial)
    assert (sim.batch_snapshot(), sim.history, sim.search_state) == (snap, before_history, before_state)

    if a_deadline == 45:
        assert stops(decoded) == [("C", "DROPOFF", 30, 2), ("A", "DROPOFF", 40, 0)]
        ScheduleValidator(full).validate(decoded)
        state = replace(baseline.state, choices=decoded.encoding.choices,
                        remaining_task_ids=tuple(row.task.task_id for row in decoded.tasks))
        commit_passengers(sim, snap, {}, replace(baseline, best_solution=decoded, state=state))
        after = sim.batch_snapshot()
        assert after.vehicles["V"].motion == vehicle.motion
        assert sim.history[:len(before_history)] == before_history
        for rid in ("A", "C"):
            assert (after.requests[rid].plan, after.requests[rid].bindings,
                    after.requests[rid].first_pickup_at) == (
                        snap.requests[rid].plan, snap.requests[rid].bindings, 0)
    else:
        assert not decoded.feasible and not decoded.repairs
        assert (decoded.failure.request_id, decoded.failure.reason) == ("A", "dropoff_window")
        assert decoded.encoding.choices == source.choices

    sim.advance_to(40)
    assert {rid: req.completed_at for rid, req in sim.batch_snapshot().requests.items()} == {
        "A": 40 if a_deadline == 45 else 20, "C": 30
    }


@pytest.mark.parametrize("target,submitted_at,failure", [
    ("A", 1, None), ("B", 1, None), ("B", 0, None),
    ("A", 1, "vehicle_capacity"), ("B", 0, "vehicle_capacity"),
    ("A", 1, "pickup_window"), ("B", 0, "pickup_window"),
])
def test_insert_selects_locked_and_waiting_markers_in_same_fragment(target, submitted_at, failure):
    sim = corridor()
    a = add_party(sim, "A", 0, 0, 2, 5, 25)
    b = add_party(sim, "B", submitted_at, 1, 3, 15 if failure == "pickup_window" else 35, 55,
                  size=3 if failure == "vehicle_capacity" else 2)
    commit_initial(sim, {"A": a}, "AA")
    snap = sim.batch_snapshot()
    plans = {"B": b}
    assert snap.request_types(0) == {"A": 3, "B": 1 if submitted_at else 2}
    full = FullDecoder(sim.router, snap, plans, M3Searcher(sim.router).search(snap, plans))
    initial = full.initial_encoding(Random(1))
    assert initial.sequence == tuple("ABB")
    late_pickup = failure == "pickup_window"
    source = Encoding(initial.choices, "BAB") if late_pickup else initial
    before = full.decode(source)
    assert before.feasible and before.encoding == source
    indices = (1, 0) if target == "B" else (0, 1)
    if late_pickup:
        indices = indices[::-1]

    trial, targets = insert(full, source, *indices)
    assert trial.sequence == tuple("ABB" if late_pickup else "BAB")
    assert targets == [(target,)]
    decoded = full.decode(trial)
    assert decoded == PartitionedDecoder(full).decode(trial)
    ScheduleValidator(full).validate(decoded)
    assert decoded.encoding.choices["A"] == source.choices["A"]
    assert sim.batch_snapshot() == snap

    if failure is None:
        assert not decoded.repairs and decoded.encoding == trial
        assert stops(decoded) == [("B", "PICKUP", 10, 4), ("A", "DROPOFF", 20, 2),
                                  ("B", "DROPOFF", 30, 0)]
    else:
        repair, = decoded.repairs
        assert (repair.request_id, repair.reason) == ("B", failure)
        assert decoded.encoding.sequence == ("A",) and "B" not in decoded.encoding.choices
        assert stops(decoded) == [("A", "DROPOFF", 20, 0)]
