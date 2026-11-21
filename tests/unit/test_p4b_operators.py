"""Hand-checked Table 3 moves on immutable two-layer encodings."""
from collections import Counter
from random import Random

import pytest

from src.m2.passengers import Plan
from src.m3 import M3Searcher
from src.m4 import Choice, Encoding, FullDecoder
from tests.p4_examples import batch, encoding, leg, release, setup


def apply(name, decoder, enc, rng, fragments=(), m=1):
    from src.m4.operators import apply_operator
    return apply_operator(name, decoder, enc, rng, fragments=fragments, m=m)


def exchange_scene():
    sim = setup([(0, 1, 1), (1, 2, 1), (2, 0, 2)], [("x", 0, 4), ("y", 0, 4)])
    options = {}
    for rid in "HABE":
        limits = {"pickup_tolerance_s": 0} if rid == "E" else {}
        direct = release(sim, rid, [leg("direct", 0, 2, 2)], **limits)
        multi = Plan("via-" + rid, rid, (leg("one", 0, 1, 1), leg("two", 1, 2, 1)))
        missing = Plan("no-flight-" + rid, rid, (leg("air", 0, 2, 1, "FLIGHT"),))
        options[rid] = (direct, multi, missing)
    options["U"] = (release(sim, "U", [leg("air", 0, 2, 1, "FLIGHT")]),)
    sim.accept("H", options["H"][0], ("x",))
    sim.advance_to(1)
    snap = sim.batch_snapshot()
    decoder = FullDecoder(sim.router, snap, options, M3Searcher(sim.router).search(snap, options))
    return sim, decoder, encoding({r: ("x",) for r in "HAB"}, "AHBBHA")


def test_plan_exchange_grows_and_shrinks_counts_and_preserves_other_tasks():
    sim, decoder, original = exchange_scene()
    before = sim.batch_snapshot()
    grown = apply("p_exchange", decoder, original, Random(12))
    changed, = [rid for rid in original.choices if grown.choices[rid] != original.choices[rid]]
    assert changed in ("A", "B")
    assert grown.choices[changed].plan_id == "via-" + changed
    assert len(grown.choices[changed].resources) == 2
    assert Counter(grown.sequence) == {rid: 4 if rid == changed else 2 for rid in "HAB"}
    others = tuple(r for r in original.sequence if r != changed)
    assert tuple(r for r in grown.sequence if r != changed) == others
    # Both exchanges affect only non-committed requests when m exceeds the pool.
    both = apply("p_exchange", decoder, original, Random(41), m=5)
    assert both.choices["H"] == original.choices["H"]
    assert (both.choices["A"].plan_id, both.choices["B"].plan_id) == ("via-A", "via-B")
    smaller = apply("p_exchange", decoder, grown, Random(12))
    assert smaller.choices[changed].plan_id == "p-" + changed
    assert Counter(smaller.sequence) == Counter(original.sequence)
    assert tuple(r for r in smaller.sequence if r != changed) == others
    assert sim.batch_snapshot() == before and original.sequence == tuple("AHBBHA")


def test_vehicle_exchange_exact_request_count_and_all_binding_alternatives():
    _, decoder, original = exchange_scene()
    one = apply("v_exchange", decoder, original, Random(5))
    assert one.sequence == original.sequence and one.choices["H"] == original.choices["H"]
    assert sorted(one.choices[r].resources for r in "AB") == [("x",), ("y",)]
    two = apply("v_exchange", decoder, original, Random(5), m=2)
    assert two.choices == {"A": Choice("p-A", ("y",)), "B": Choice("p-B", ("y",)),
                           "H": original.choices["H"]}
    via = Encoding({"A": Choice("via-A", ("x", "x")), "H": original.choices["H"]}, "AHHAAA")
    outcomes = {apply("v_exchange", decoder, via, Random(i)).choices["A"].resources for i in range(30)}
    assert outcomes == {("x", "y"), ("y", "x"), ("y", "y")}


def test_restart_restores_dropped_valid_requests_reselects_plans_and_uses_baseline():
    _, decoder, original = exchange_scene()
    dropped = original.without("A").without("B")
    assert "A" not in dropped.choices and decoder.snapshot.request_types(0)["A"] == 2
    outcomes = [apply("restart", decoder, dropped, Random(seed)) for seed in range(20)]
    for enc in outcomes:
        assert set(enc.choices) == {"H", "A", "B"}
        assert enc.choices["H"] == original.choices["H"] and enc.sequence[:2] == ("H", "H")
        for rid in "AB":
            expected = {"p-" + rid: 2, "via-" + rid: 4}
            assert enc.sequence.count(rid) == expected[enc.choices[rid].plan_id]
    assert {enc.choices["A"].plan_id for enc in outcomes} == {"p-A", "via-A"}


class PositionDraws:
    """Specify positions so expected moves can be derived without using RNG internals."""
    def __init__(self, endpoints):
        self.endpoints = endpoints

    def sample(self, population, k):
        return [population[i] for i in self.endpoints]

    def choice(self, population):
        return population[self.endpoints[0]]

    def randrange(self, stop):
        return self.endpoints[-1]


@pytest.mark.parametrize("name,endpoints,first,second", [
    ("insert", (0, 3), "CACA", "DBDB"),
    ("cut", (1, 2), "ACCA", "BDDB"),
    ("swap", (0, 3), "CCAA", "DDBB"),
    ("reverse", (0, 3), "CACA", "DBDB"),
])
def test_position_move_exact_on_each_interleaved_fragment(name, endpoints, first, second):
    sim = setup([(0, 1, 1), (1, 0, 1)], [("x", 0, 4), ("y", 0, 4)])
    decoder = batch(sim, *(release(sim, r, [leg("l", 0, 1, 1)]) for r in "ABCD"))
    enc = encoding({r: ("x" if r in "AC" else "y",) for r in "ABCD"}, "ABCDABCD")
    parts = ((0, 2, 4, 6), (1, 3, 5, 7))
    moved = apply(name, decoder, enc, PositionDraws(endpoints), parts)
    assert moved.sequence[::2] == tuple(first)
    assert moved.sequence[1::2] == tuple(second)
    assert moved.choices == enc.choices and decoder.decode(moved).feasible


def test_insert_never_selects_committed_task_as_direct_target():
    _, decoder, enc = exchange_scene()
    # First eligible item is A even when the first position belongs to H.
    enc = Encoding(enc.choices, "HABBAH")
    moved = apply("insert", decoder, enc, PositionDraws((0, 5)), (tuple(range(6)),))
    assert moved.sequence == tuple("HBBAHA")
    locked = decoder.safe_baseline.encoding
    assert apply("insert", decoder, locked, Random(9), ((0, 1),)) == locked


def test_plan_exchange_inserts_and_deletes_the_selected_occurrences():
    _, decoder, original = exchange_scene()

    class CountDraws:
        def sample(self, population, k):
            if k == 1:
                return [population[0]]  # choose A to exchange
            return [population[0], population[2]]  # remove A's first and third occurrences

        def choice(self, population):
            return population[0]

        def randrange(self, stop):
            return 2

    grown = apply("p_exchange", decoder, original, CountDraws())
    assert grown.sequence == tuple("AHAABBHA")
    shrunk = apply("p_exchange", decoder, grown, CountDraws())
    assert shrunk.sequence == tuple("HABBHA")


@pytest.mark.parametrize("name,endpoints", [
    ("insert", (0, 0)),  # the first eligible request is A at index zero
    ("swap", (2, 3)), ("reverse", (2, 3)),  # the two adjacent Bs
    ("cut", (2, 2)), ("swap", (2, 2)), ("reverse", (2, 2)),
    ("cut", (0, 5)),  # moving the whole segment to the end preserves its order
])
def test_identity_positional_moves(name, endpoints):
    _, decoder, enc = exchange_scene()
    assert apply(name, decoder, enc, PositionDraws(endpoints), (tuple(range(6)),)) == enc


@pytest.mark.parametrize("name", ["p_exchange", "v_exchange", "restart", "insert", "cut", "swap", "reverse"])
def test_seven_operators_replay_with_explicit_rng_and_handle_empty_segments(name):
    _, decoder, enc = exchange_scene()
    fragments = ((), tuple(range(len(enc.sequence))))
    assert apply(name, decoder, enc, Random(123), fragments) == apply(name, decoder, enc, Random(123), fragments)
    if name != "restart":
        base = decoder.safe_baseline.encoding
        assert apply(name, decoder, base, Random(123), ((), (0,), (1,))) == base
