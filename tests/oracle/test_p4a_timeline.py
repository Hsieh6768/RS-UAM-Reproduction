from dataclasses import replace
from decimal import Decimal

import pytest

from src.m2.passengers import FlightInstance, WalkLink
from src.m4 import ScheduleValidator, ValidationError
from tests.p4_examples import batch, encoding, leg, release, setup


def test_fig5_shared_car_flight_and_walk_timeline(tmp_path):
    sim = setup([(i, i+1, 2) for i in range(8)], [("x", 0, 4), ("y", 5, 4)],
                [FlightInstance("f", 3, 5, 12, 16)])
    steps = (WalkLink("w0", 0, 1, 2, 1), leg("c1", 1, 2, 2),
             WalkLink("w1", 2, 3, 3, 1), leg("air", 3, 5, 4, "FLIGHT"),
             leg("c2", 5, 6, 2), WalkLink("w2", 6, 7, 1, 1), WalkLink("w3", 7, 8, 2, 1))
    a = release(sim, "A", steps, size=2)
    b = release(sim, "B", [leg("b", 1, 2, 2)])
    decoder = batch(sim, a, b)
    candidate = encoding({"A": ("x", "f", "y"), "B": ("x",)}, "ABABAAA")
    out = decoder.decode(candidate)
    assert out.feasible and not out.repairs
    assert [(s.task.request_id, s.task.kind, s.at, s.onboard_after) for s in out.vehicles["x"]] == [
        ("A", "PICKUP", 2, 2), ("B", "PICKUP", 2, 3),
        ("A", "DROPOFF", 4, 1), ("B", "DROPOFF", 4, 0)]
    assert [(s.ready, s.pickup, s.dropoff) for s in out.trips["A"]] == [
        (0, 0, 2), (2, 2, 4), (4, 4, 7), (7, 12, 16),
        (16, 16, 18), (18, 18, 19), (19, 19, 21)]
    assert out.completion == {"A": 21, "B": 4}
    assert out.f1 == 200 and out.f2 == pytest.approx(25 / 60)
    assert out.v == pytest.approx(139.875)
    assert out.flight_seats["f"] == {"A": 2}
    audit = ScheduleValidator(decoder, failure_dir=tmp_path)
    audit.validate(out)
    tampered = [
        replace(out, v=140),
        replace(out, flight_seats={"f": {"B": 2}}),
        replace(out, vehicles={**out.vehicles, "x": tuple(reversed(out.vehicles["x"]))}),
        replace(out, trips={**out.trips, "A": (*out.trips["A"][:-1], replace(out.trips["A"][-1], pickup=20))}),
    ]
    flight_row = next(row for row in out.tasks if row.task.kind == "FLIGHT")
    ghost = replace(flight_row, task=replace(flight_row.task, request_id="ghost"))
    tampered.append(replace(out, tasks=out.tasks + (ghost,),
                            encoding=replace(out.encoding, sequence=out.encoding.sequence + ("ghost",))))
    for invalid in tampered:
        with pytest.raises(ValidationError):
            audit.validate(invalid)
    assert len(list(tmp_path.glob("*.json"))) == len(tampered)


def test_two_car_connection_and_absolute_fractional_edge_time():
    sim = setup([(0, 1, ".4"), (1, 2, ".4"), (2, 3, ".4"), (3, 4, ".75"), (4, 5, 5),
                 (5, 6, 7)], [("x", 0, 4), ("y", 5, 4)])
    a = release(sim, "A", [leg("a", 4, 5, 5), leg("b", 5, 6, 7)])
    from src.simulation.events import CandidateRoute
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "x", 3))
    sim.advance_to(1)
    decoder = batch(sim, a)
    out = decoder.decode(encoding({"A": ("x", "y")}, "AAAA"))
    assert [(s.pickup, s.dropoff) for s in out.trips["A"]] == [(2, 7), (7, 14)]
    assert out.vehicles["x"][0].begin == Decimal("1.2")
    assert [s.at for s in out.vehicles["y"]] == [7, 14]
    ScheduleValidator(decoder).validate(out)


def test_all_two_party_orders_against_hand_computed_stop_times():
    # On the bidirectional line 0--1--2 each hop takes 5 s.
    # A goes 0->2, B goes 1->2. Deliberate detours change both parties' clocks.
    sim = setup([(0, 1, 5), (1, 0, 5), (1, 2, 5), (2, 1, 5)], [("v", 0, 4)])
    a = release(sim, "A", [leg("a", 0, 2, 10)])
    b = release(sim, "B", [leg("b", 1, 2, 5)], size=2)
    decoder = batch(sim, a, b)
    expected = {
        "AABB": ([0, 10, 15, 20], [1, 0, 2, 0]),
        "ABAB": ([0, 5, 10, 10], [1, 3, 2, 0]),
        "ABBA": ([0, 5, 10, 10], [1, 3, 1, 0]),
        "BAAB": ([5, 10, 20, 20], [2, 3, 2, 0]),
        "BABA": ([5, 10, 20, 20], [2, 3, 1, 0]),
        "BBAA": ([5, 10, 20, 30], [2, 0, 1, 0]),
    }
    for seq, (times, loads) in expected.items():
        result = decoder.decode(encoding({"A": ("v",), "B": ("v",)}, seq))
        assert [s.at for s in result.tasks] == times
        assert [s.onboard_after for s in result.tasks] == loads
        ScheduleValidator(decoder).validate(result)
