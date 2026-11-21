"""Exhaustive small interleavings exercise repair ordering and cache isolation."""
from itertools import permutations

from src.m2.passengers import FlightInstance, Plan
from src.m3 import M3Searcher
from src.m4 import Choice, Encoding, FullDecoder, ScheduleValidator
from tests.p4_examples import batch, encoding, leg, release, setup


def test_all_90_interleavings_and_plan_window_change_on_one_frozen_batch():
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, "1.2"), (1, 0, "1.4")], [("x", 0, 1), ("y", 0, 1)])
    plans = {r: (release(sim, r, [leg("ride", 0, 1, "1.2")]),) for r in "ABC"}
    # Both plans have identical stable task IDs and use the same resource;
    # only the plan ID/window changes. A cached completion at 2 cannot serve
    # as the answer for the plan whose dropoff is due by 1.
    plans["A"] += (Plan("urgent", "A", (leg("ride", 0, 1, "1.2", ld=1),)),)
    snap = sim.batch_snapshot()
    full = FullDecoder(sim.router, snap, plans, M3Searcher(sim.router).search(snap, plans))
    split = PartitionedDecoder(full)
    valid = repaired = 0
    for seq in sorted(set(permutations("AABBCC"))):
        for plan in plans["A"]:
            assignments = {rid: Choice(plans[rid][0].plan_id, ("x" if rid == "B" else "y",))
                           for rid in "BC"}
            assignments["A"] = Choice(plan.plan_id, ("x",))
            encoding = Encoding(assignments, seq)
            out = split.decode(encoding)
            assert out == full.decode(encoding)
            ScheduleValidator(full).validate(out)
            assert out.completion["C"] == 2
            if out.repairs:
                repaired += 1
            else:
                valid += 1
            if plan.plan_id == "urgent":
                assert "A" not in out.completion
    assert (valid, repaired) == (30, 150)
    assert sim.batch_snapshot() == snap


def test_local_repair_rolls_back_temporary_seats_and_keeps_history_in_conflict_order():
    from src.m4 import PartitionedDecoder
    sim = setup([(0, 1, 10), (1, 2, 3), (3, 2, 1)], [("stranded", 3, 4)],
                [FlightInstance("f", 0, 1, 2, 4)])
    flight_leg = leg("f", 0, 1, 2, "FLIGHT")
    h, a = [release(sim, rid, [flight_leg], size=2) for rid in "HA"]
    b = release(sim, "B", [flight_leg, leg("car", 1, 2, 3)], size=2)
    sim.accept("H", h, ("f",))
    snap = sim.batch_snapshot()
    full = batch(sim, a, b)
    split = PartitionedDecoder(full)
    choices = {"A": ("f",), "H": ("f",), "B": ("f", "stranded")}
    for seq, dropped, seats in [
            ("BBBHA", ["B"], {"H": 2, "A": 2}),
            ("BHABB", ["A", "B"], {"H": 2})]:
        enc = encoding(choices, seq)
        out = split.decode(enc)
        assert out == full.decode(enc) and out.feasible
        assert [conflict.request_id for conflict in out.repairs] == dropped
        assert out.flight_seats["f"] == seats and out.vehicles["stranded"] == ()
        assert set(out.encoding.choices) == set(seats)
        ScheduleValidator(full).validate(out)
    assert sim.batch_snapshot() == snap
