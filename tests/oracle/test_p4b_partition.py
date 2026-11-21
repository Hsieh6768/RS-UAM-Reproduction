from random import Random
from dataclasses import replace
import json

import pytest

from src.m2.passengers import FlightInstance, WalkLink, task_id
from src.m4 import ScheduleValidator
from tests.p4_examples import batch, encoding, leg, release, setup


def independent_example():
    sim = setup([(0, 1, 2), (1, 0, 2)], [("x", 0, 4), ("y", 0, 4), ("idle", 1, 4)],
                [FlightInstance("f", 0, 1, 10, 13)])
    a, b = [release(sim, rid, [leg("c", 0, 1, 2)]) for rid in "AB"]
    c, d = [release(sim, rid, [leg("air", 0, 1, 3, "FLIGHT")], size=2) for rid in "CD"]
    return batch(sim, a, b, c, d), {"A": ("x",), "B": ("y",), "C": ("f",), "D": ("f",)}


def test_typed_graph_and_interleaved_component_ledgers():
    from src.m4.task_graph import TaskGraph
    from src.m4.partitioned_decoder import PartitionedDecoder
    full, bindings = independent_example()
    enc = encoding(bindings, "ACBDAB")
    graph = TaskGraph.build(full, enc)
    assert [(c.request_ids, c.positions) for c in graph.components] == [
        (("A",), (0, 4)), (("C", "D"), (1, 3)), (("B",), (2, 5))]
    c, d = (task_id(r, "air", "FLIGHT") for r in "CD")
    p, q = (task_id("A", "c", kind) for kind in ("PICKUP", "DROPOFF"))
    assert {(e.source, e.target, e.kind) for e in graph.edges if e.source in (c, d, p)} == {
        (c, d, "flight_capacity"), (d, c, "flight_capacity"),
        (p, q, "vehicle_order"), (p, q, "request_order")}
    split = PartitionedDecoder(full)
    result = split.decode(enc)
    assert [s.at for s in result.tasks] == [0, 13, 0, 13, 2, 2]
    assert result.flight_seats["f"] == {"C": 2, "D": 2}
    assert result.final_nodes == {"x": 1, "y": 1, "idle": 1}
    assert result.v == pytest.approx(280 - .3 * 30/60)
    assert result == full.decode(enc)
    ScheduleValidator(full).validate(result)
    changed = encoding({**bindings, "B": ("x",)}, "ACBDAB")
    assert split.decode(changed) == full.decode(changed)
    assert split.last_stats.reused_components == 1
    assert split.last_stats.evaluated_components == 1


def test_zero_remaining_type3_shared_seats_and_preserved_appointments():
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, 2)], flights=[FlightInstance("f", 0, 1, 2, 9)])
    h = release(sim, "H", [leg("air", 0, 1, 7, "FLIGHT")], size=2)
    sim.accept("H", h, ("f",))
    sim.advance_to(4)
    full = batch(sim)
    result = PartitionedDecoder(full).decode(full.safe_baseline.encoding, preserve_appointments=True)
    assert result == full.safe_baseline
    assert not result.tasks and result.completion == {"H": 9}
    assert result.flight_seats["f"] == {"H": 2}


def test_interleaved_conflicts_follow_global_task_order_and_reuse_independent_component():
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, 2), (1, 0, 2)], [(rid, 0, 4) for rid in "xyz"])
    a, b = [release(sim, rid, [leg("c", 0, 1, 2, ld=1)]) for rid in "AB"]
    c = release(sim, "C", [leg("c", 0, 1, 2)])
    full = batch(sim, a, b, c)
    split = PartitionedDecoder(full)
    enc = encoding({"A": ("x",), "B": ("y",), "C": ("z",)}, "ABCBAC")
    result = split.decode(enc)
    assert [r.request_id for r in result.repairs] == ["B", "A"]
    assert result == full.decode(enc)
    assert result.completion == {"C": 2}
    assert split.last_stats.repair_order == result.repairs
    assert split.last_stats.evaluated_components == 3


def test_local_repair_can_split_one_component_and_retains_other_schedules():
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, 2), (1, 0, 2)], [(rid, 0, 4) for rid in "xyz"])
    bridge = release(sim, "Q", [leg("go", 0, 1, 2, ld=1), leg("back", 1, 0, 2)])
    plans = [release(sim, r, [leg("c", 0, 1, 2)]) for r in "ABC"]
    full = batch(sim, bridge, *plans)
    split = PartitionedDecoder(full)
    enc = encoding({"A": ("x",), "B": ("y",), "C": ("z",), "Q": ("x", "y")}, "QQQQABCABC")
    result = split.decode(enc)
    assert result == full.decode(enc)
    assert result.completion == {"A": 2, "B": 2, "C": 2}
    assert result.repairs[0].request_id == "Q"
    assert sorted(split.last_stats.component_sizes) == [2, 2, 2]
    assert split.last_stats.evaluated_components == 4


def test_type3_component_failure_invalidates_whole_candidate_and_preserves_snapshot():
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, 2), (1, 2, 20), (2, 1, 20)], [("v", 0, 4), ("w", 0, 4)])
    h = release(sim, "H", [leg("h", 0, 1, 2, ld=4)])
    a = release(sim, "A", [leg("a", 1, 2, 20)])
    b = release(sim, "B", [leg("b", 0, 1, 2)])
    sim.accept("H", h, ("v",))
    before = sim.batch_snapshot()
    full = batch(sim, a, b)
    enc = encoding({"H": ("v",), "A": ("v",), "B": ("w",)}, "HABABH")
    result = PartitionedDecoder(full).decode(enc)
    assert result == full.decode(enc)
    assert not result.feasible and result.failure.request_id == "H"
    assert result.v is None and not result.tasks and not result.completion
    assert sim.batch_snapshot() == before


def test_cooperative_checkpoints_interrupt_graph_sampling_and_both_decoders():
    from src.m4.task_graph import TaskGraph
    from src.m4.partitioned_decoder import PartitionedDecoder
    full, bindings = independent_example()
    enc = encoding(bindings, "ACBDAB")
    def stop():
        raise TimeoutError
    for action in (lambda: TaskGraph.build(full, enc, checkpoint=stop),
                   lambda: full.initial_encoding(Random(3), checkpoint=stop),
                   lambda: full.decode(enc, checkpoint=stop),
                   lambda: PartitionedDecoder(full).decode(enc, checkpoint=stop)):
        with pytest.raises(TimeoutError):
            action()
    assert full.initial_encoding(Random(3)) == full.initialize(Random(3)).random_candidate.encoding


def test_stable_identity_and_cross_walk_edges_on_mixed_fractional_directed_network():
    from src.m4.task_graph import TaskGraph
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, "1.5"), (1, 2, "1.25"), (2, 3, 1), (3, 4, 1), (4, 5, "3.25"), (5, 6, 1)],
                [("x", 1, 4), ("y", 4, 4), ("u", 0, 4), ("stranded", 6, 4)],
                [FlightInstance("f", 3, 4, 8, 10)])
    a = release(sim, "A", [WalkLink("w0", 0, 1, 2, 1), leg("x", 1, 2, "1.25"),
                           WalkLink("w1", 2, 3, 2, 1), leg("air", 3, 4, 2, "FLIGHT"),
                           leg("y", 4, 5, "3.25"), WalkLink("w2", 5, 6, 1, 1)])
    b = release(sim, "B", [leg("air", 3, 4, 2, "FLIGHT")], size=2)
    c, d = [release(sim, rid, [leg("line", 0, 1, "1.5")]) for rid in "CD"]
    full = batch(sim, a, b, c, d)
    split = PartitionedDecoder(full)
    choices = {"A": ("x", "f", "y"), "B": ("f",), "C": ("u",)}
    enc = encoding(choices, "BACACAAA")
    reordered = encoding(choices, "CABACAAA")
    for candidate in (enc, reordered):
        graph = TaskGraph.build(full, candidate)
        assert {c.component_id for c in graph.components} == {("A", "B"), ("C",)}
        links = {(e.source, e.target) for e in graph.edges if e.kind == "request_order"}
        assert (task_id("A", "x", "DROPOFF"), task_id("A", "air", "FLIGHT")) in links
        assert (task_id("A", "air", "FLIGHT"), task_id("A", "y", "PICKUP")) in links
        result = split.decode(candidate)
        assert [(t.ready, t.pickup, t.dropoff) for t in result.trips["A"]] == [
            (0, 0, 2), (2, 2, 4), (4, 4, 6), (6, 8, 10), (10, 10, 14), (14, 14, 15)]
        assert result.flight_seats["f"] == {"B": 2, "A": 1}
        assert result.completion == {"A": 15, "B": 10, "C": 2}
        assert result == full.decode(candidate)
        ScheduleValidator(full).validate(result)
    stranded = encoding({**choices, "D": ("stranded",)}, "DBACACAAAD")
    repaired = split.decode(stranded)
    assert repaired == full.decode(stranded)
    assert [(c.request_id, c.reason) for c in repaired.repairs] == [("D", "unreachable_road")]


def test_onboard_type3_starts_next_task_after_current_edge_and_replays_committed_appointment():
    from src.m4.partitioned_decoder import PartitionedDecoder
    from src.simulation.events import Action, CandidateRoute
    sim = setup([(0, 1, 20), (1, 2, 2)], [("v", 0, 4), ("w", 1, 4)])
    h = release(sim, "H", [leg("r", 0, 1, 20)], size=4)
    b = release(sim, "B", [leg("b", 1, 2, 2)])
    sim.accept("H", h, ("v",))
    pick, drop = (task_id("H", "r", k) for k in ("PICKUP", "DROPOFF"))
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "v", actions=(
        Action(0, "PICKUP", pick), Action(0, "DRIVE", 1), Action(25, "DROPOFF", drop))))
    sim.advance_to(10)
    full = batch(sim, b)
    split = PartitionedDecoder(full)
    assert split.decode(full.safe_baseline.encoding, preserve_appointments=True) == full.safe_baseline
    assert full.safe_baseline.completion == {"H": 25}
    enc = encoding({"H": ("v",), "B": ("w",)}, "BHB")
    result = split.decode(enc)
    assert result.vehicles["v"][0].task.task_id == drop
    assert result.vehicles["v"][0].begin == 20
    assert result.completion == {"H": 20, "B": 12}
    assert result == full.decode(enc)
    ScheduleValidator(full).validate(result)


@pytest.mark.parametrize("damage", ("missing", "duplicate", "extra", "passenger"))
def test_corrupt_component_output_raises_with_snapshot_evidence(tmp_path, monkeypatch, damage):
    from src.m4 import ValidationError
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, 2)], [("x", 0, 4)])
    a = release(sim, "A", [leg("c", 0, 1, 2)])
    full = batch(sim, a, failure_dir=tmp_path)
    split = PartitionedDecoder(full)
    original = split._local
    def corrupt(*args):
        rows, cars, nodes, seats, trips, ends = original(*args)
        if damage == "missing":
            rows = rows[:-1]
        elif damage == "duplicate":
            rows += rows[:1]
        elif damage == "extra":
            fake = replace(rows[0].task, task_id="unknown")
            rows += (replace(rows[0], task=fake),)
        else:
            ends = {}
        return rows, cars, nodes, seats, trips, ends
    monkeypatch.setattr(split, "_local", corrupt)
    with pytest.raises(ValidationError) as error:
        split.decode(encoding({"A": ("x",)}, "AA"))
    report = json.loads(error.value.failure_path.read_text(encoding="utf-8"))
    assert report["status"] == "FAILED" and report["snapshot"]["time"] == 0
