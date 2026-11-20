from random import Random
from dataclasses import replace

import pytest

from src.m2.passengers import FlightInstance, Plan
from src.m3 import M3Searcher
from src.m4 import Choice, Encoding, FullDecoder
from src.m4 import EncodingError, ScheduleValidator
from tests.p4_examples import batch, encoding, leg, release, setup


@pytest.mark.parametrize("ids,resources", [("A", ("x",)), ("AAA", ("x",)),
                                          ("AA", ("missing",)), ("AA", ()), ("AZ", ("x",))])
def test_encoding_contract(ids, resources):
    sim = setup([(0, 1, 10)], [("x", 0, 4)])
    a = release(sim, "A", [leg("a", 0, 1, 10)])
    with pytest.raises(EncodingError):
        batch(sim, a).decode(encoding({"A": resources}, ids))


@pytest.mark.parametrize("now,kept", [(400, True), (405, False)])
def test_separate_lp_and_ld_at_actual_events(now, kept):
    sim = setup([(0, 1, 10)], [("x", 0, 4)])
    a = release(sim, "A", [leg("a", 0, 1, 10, lp=420, ld=410)])
    sim.advance_to(now)
    result = batch(sim, a).decode(encoding({"A": ("x",)}, "AA"))
    assert result.feasible and ("A" in result.completion) == kept
    if kept:
        assert result.trips["A"][0].pickup == 400 and result.completion["A"] == 410
    else:
        assert result.repairs[0].reason == "dropoff_window"


def test_minutes_objective_and_seeded_low_value_initialization():
    sim = setup([(0, 1, 600), (1, 2, 16000)], [("x", 0, 4)])
    a = release(sim, "A", [leg("a", 0, 1, 600)])
    b = release(sim, "B", [leg("b", 0, 2, 16600, lp=2000, ld=20000)], pickup_tolerance_s=2000)
    decoder = batch(sim, a, b)
    out = decoder.decode(encoding({"A": ("x",)}, "AA"))
    assert (out.f1, out.f2, out.v) == (100, 10, 67)
    only_b = batch(sim, b)
    init = only_b.initialize(Random(23))
    assert init == only_b.initialize(Random(23))
    assert init.current_solution.v < 0 and init.best_solution == init.safe_baseline
    assert init.safe_baseline.v == 0
    with pytest.raises(ValueError):
        out.better_than(init.best_solution)


def test_missed_flight_and_unreachable_repairs_are_whole_and_deterministic():
    sim = setup([(0, 1, 8), (1, 2, 5), (2, 3, 3)], [("x", 0, 4), ("y", 3, 4)],
                [FlightInstance("f", 1, 2, 5, 10)])
    a = release(sim, "A", [leg("a", 0, 1, 8), leg("f", 1, 2, 5, "FLIGHT")])
    b = release(sim, "B", [leg("f", 1, 2, 5, "FLIGHT"), leg("b", 2, 3, 3)])
    decoder = batch(sim, a, b)
    enc = encoding({"B": ("f", "y"), "A": ("x", "f")}, "AAABBB")
    result = decoder.decode(enc)
    assert [(r.request_id, r.reason) for r in result.repairs] == [
        ("A", "missed_flight"), ("B", "unreachable_road")]
    assert result.attempts == 3 and result.encoding.sequence == ()
    assert result.flight_seats["f"] == {} and result.vehicles == {"x": (), "y": ()}
    assert decoder.decode(enc) == result
    assert set(sim.batch_snapshot().request_types()) == {"A", "B"}


def test_rng_draws_only_from_first_plan_and_reproducibly_interleaves_ids():
    sim = setup([(0, 1, 10), (1, 0, 10)], [("x", 0, 4), ("y", 0, 4)])
    plans = [release(sim, rid, [leg("c", 0, 1, 10)]) for rid in "ABC"]
    catalog = {p.request_id: (p, Plan("alternative", p.request_id, p.legs)) for p in plans}
    snap = sim.batch_snapshot()
    dec = FullDecoder(sim.router, snap, catalog, M3Searcher(sim.router).search(snap, catalog))
    first = dec.initialize(Random(23)).current_solution
    assert first == dec.initialize(Random(23)).current_solution
    assert first.encoding != dec.initialize(Random(41)).current_solution.encoding
    assert all(c.plan_id == "p-" + rid for rid, c in first.encoding.choices.items())
    assert len(first.tasks) == 6 and first.f1 == 300
    ScheduleValidator(dec).validate(first)


def test_actual_pickup_deadline_independent_of_generous_dropoff_limit():
    sim = setup([(0, 1, 11), (1, 2, 1)], [("v", 0, 4)])
    p = release(sim, "A", [leg("c", 1, 2, 1, lp=10, ld=100)])
    decoder = batch(sim, p)
    out = decoder.decode(encoding({"A": ("v",)}, "AA"))
    assert out.repairs[0].reason == "pickup_window" and not out.completion


def test_defensive_copy_and_explicit_malformed_layer_errors():
    resources, seq = ["v"], ["R", "R"]
    choices = {"R": Choice("p", resources)}
    result = Encoding(choices, seq)
    resources.clear()
    seq.clear()
    choices.clear()
    assert result.sequence == ("R", "R") and result.choices["R"].resources == ("v",)
    with pytest.raises(EncodingError):
        Encoding(result.choices, [["R"]])
    with pytest.raises(EncodingError):
        Choice("p", None)


def test_stale_sparse_rows_and_direction_are_checked_at_decode_boundary():
    sim = setup([(0, 1, 10)], flights=[FlightInstance("wrong", 1, 0, 10, 15)])
    p = release(sim, "A", [leg("f", 0, 1, 5, "FLIGHT")])
    snapshot = sim.batch_snapshot()
    plans = {"A": (p,)}
    sparse = M3Searcher(sim.router).search(snapshot, plans)
    assert sparse.by_leg["A", p.plan_id, "f"] == ()
    with pytest.raises(ValueError, match="Stale"):
        FullDecoder(sim.router, snapshot, plans, replace(sparse, snapshot_version=snapshot.version + 1))
    injected = replace(sparse, by_leg={("A", p.plan_id, "f"): ("wrong",)})
    decoder = FullDecoder(sim.router, snapshot, plans, injected)
    out = decoder.decode(encoding({"A": ("wrong",)}, "A"))
    assert out.repairs[0].reason == "incompatible_flight" and not out.flight_seats["wrong"]
