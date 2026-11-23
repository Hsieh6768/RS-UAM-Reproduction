"""D004/D011–D014. Entirely synthetic, including the two-hour clock boundary."""
import pytest

from src.m1.config import M1Config
from src.m2.passengers import Request
from src.m3.config import M3Config
from src.m4.halns import M4Config
from src.m5.rebalancer import M5Config
from src.simulation.engine import Simulator
from src.simulation.rolling import run_rolling
from tests.p2_examples import network


class Collect:
    def __init__(self):
        self.rows = []
        self.history = []

    def batch(self, row):
        self.rows.append(row)

    def events(self, events):
        self.history.extend(events)


def scenario(directory, demand, *, evaluation=(0, 20), coverage=1000, cars=(0,),
             road_time=17, interval=10, validator=True, flights=False, tol=45,
             rebalance=True, net=None, profile_run=None, diagnostic_run=None):
    if net is None:
        net = network([0, .01], [(0, 1, road_time), (1, 0, road_time)],
                      (0, 1) if flights else (), [(0, 1, 5)] if flights else ())
    sim = Simulator(net.road, batch_seconds=interval, failure_dir=directory)
    for index, node in enumerate(cars):
        sim.add_vehicle(str(index), node)
    loaded = []

    def envelope(at, rid, start, end):
        def release():
            loaded.append((rid, sim.now))
            return Request(rid, at, start, end, 1, net.road.shortest_time_s(start, end),
                           pickup_tolerance_s=tol, delay_tolerance_s=tol)
        return at, rid, release

    out = Collect()
    snap = run_rolling(sim, net, (envelope(*row) for row in demand),
                       eval_start=evaluation[0], eval_end=evaluation[1], coverage_end=coverage,
                       seed=32, batch_seconds=interval, m1_config=M1Config(), m3_config=M3Config(),
                       m4_config=M4Config(outer_loops_per_request=1, inner_loops=2),
                       m5_config=M5Config(enabled=rebalance), validator_enabled=validator,
                       reporter=out, failure_dir=directory, profile_run=profile_run,
                       diagnostic_run=diagnostic_run)
    return snap, out.rows, out.history, loaded


def test_exact_release_between_batches_and_physical_completion(tmp_path):
    snap, rows, history, loaded = scenario(tmp_path, [(3, "R", 0, 1)])
    req = snap.requests["R"]
    assert loaded == [("R", 3)]
    assert (req.submitted_at, req.accepted_at, req.first_pickup_at, req.completed_at) == (3, 10, 10, 27)
    assert [(event.kind.value, event.time) for event in history if event.kind == "EDGE_END"] == [("EDGE_END", 27)]
    assert [(r["batch_time"], r["type1"], r["type3"]) for r in rows] == [
        (0, 0, 0), (10, 1, 0), (20, 0, 1), (30, 0, 0)]
    assert [r["accepted_new"] for r in rows] == [0, 1, 0, 0]
    assert [r["m5_reason"] for r in rows][1] == "NO_SUPPLY"
    assert all(r["validator_enabled"] for r in rows)


def test_deadline_inclusive_then_expire_and_cache_type2(tmp_path):
    snap, rows, history, _ = scenario(tmp_path, [(0, "R", 0, 1)], cars=(), tol=10)
    assert snap.requests["R"].status == "EXPIRED_UNSERVED"
    assert [r["type2"] for r in rows] == [0, 1, 0]
    assert [r["m1_planned"] for r in rows] == [1, 0, 0]
    assert [r["m1_cache_hits"] for r in rows] == [0, 1, 0]
    assert [e.time for e in history if e.kind == "EXPIRE"] == [11]
    assert sum(r["expired"] for r in rows) == 1


def test_short_window_tail_and_post_window_acceptance(tmp_path):
    snap, rows, _, loaded = scenario(tmp_path, [
        (7, "eval", 0, 1), (8, "tail", 0, 1), (150, "too-late", 0, 1)
    ], evaluation=(0, 8), cars=(0, 0))
    assert loaded == [("eval", 7), ("tail", 8)]
    req = snap.requests["eval"]
    assert req.cohort == "EVALUATION" and req.accepted_at == 10 and req.completed_at is not None
    assert snap.requests["tail"].cohort == "TAIL"
    assert rows[1]["type1"] == 2 and rows[1]["m1_planned"] == 2
    assert rows[-1]["evaluation_completed"] == rows[-1]["evaluation_accepted"] == 1


def test_intake_stops_at_terminal_event_between_decisions(tmp_path):
    snap, _, _, loaded = scenario(tmp_path, [(0, "eval", 0, 1), (18, "closed", 0, 1)],
                                  evaluation=(0, 10))
    assert loaded == [("eval", 0)]
    assert snap.requests["eval"].completed_at == 17


def test_exactly_two_hours_warmup_preserves_passenger_into_evaluation(tmp_path):
    snap, rows, _, _ = scenario(tmp_path, [(0, "warm", 0, 1), (7200, "eval", 0, 1)],
                                evaluation=(7200, 7210), coverage=20000, interval=3600,
                                cars=(0, 0), road_time=7205, tol=45)
    assert snap.requests["warm"].cohort == "WARMUP"
    assert snap.requests["warm"].completed_at == 7205
    at_boundary = next(row for row in rows if row["batch_time"] == 7200)
    assert at_boundary["type3"] == at_boundary["type1"] == 1
    assert rows[-1]["evaluation_accepted"] == rows[-1]["evaluation_completed"] == 1


def test_insufficient_tail_coverage_detected_at_source_boundary(tmp_path):
    with pytest.raises(ValueError, match="coverage"):
        scenario(tmp_path, [(0, "eval", 0, 1)], evaluation=(0, 5), coverage=15, road_time=17)


def test_fixed_flight_phase_and_ids_advance_once(tmp_path):
    snap, _, events, _ = scenario(tmp_path, [], evaluation=(0, 200), interval=10, flights=True)
    assert set(snap.flights) == {"0>1:0", "0>1:1"}
    assert [(e.object_id, e.time) for e in events if e.kind == "DEPART"] == [("0>1:0", 0), ("0>1:1", 180)]
    assert [e.time for e in events if e.kind == "LAND"] == [5, 185]


def test_no_r_and_validator_disabled_and_repeatable(tmp_path):
    args = [(10, "R", 0, 1)]
    a, rows, events, _ = scenario(tmp_path, args, rebalance=False, validator=False)
    b, _, other, _ = scenario(tmp_path, args, rebalance=False, validator=False)
    assert a.requests == b.requests and events == other
    assert all(row["m5_status"] == "SKIPPED" and row["m5_reason"] == "DISABLED"
               and row["rebalanced"] == 0 and row["validator_wall_ms"] == 0 for row in rows)


def test_actual_m4_then_m5_transport_then_next_batch_passenger_dispatch(tmp_path):
    # A ties up a car at node 0. B's pickup at node 1 is outside the M3
    # search radius at t=0. Only the second idle car may receive the M5 move.
    snap, rows, events, _ = scenario(tmp_path, [(0, "A", 0, 1), (0, "B", 1, 0)], cars=(0, 0))
    first = rows[0]
    assert (first["accepted_new"], first["m5_status"], first["m5_supply"], first["rebalanced"]) == (1, "READY", 1, 1)
    assert (rows[1]["m5_supply"], rows[1]["fleet_rebalancing"]) == (0, 1)
    assert [(snap.requests[r].accepted_at, snap.requests[r].completed_at) for r in ("A", "B")] == [
        (0, 17), (20, 37)]
    assert sum(e.kind == "EDGE_END" and e.time == 17 for e in events) == 2
    assert rows[-1]["evaluation_completed"] == 2


def test_solver_failure_is_observable_and_retains_passenger_schedule(tmp_path, monkeypatch):
    from src.m5.transport import TransportResult
    monkeypatch.setattr("src.m5.rebalancer.solve_transport",
                        lambda *args: TransportResult("BALANCED", "LICENSE_ERROR", detail="synthetic"))
    snap, rows, _, _ = scenario(tmp_path, [(0, "A", 0, 1), (0, "B", 1, 0)], cars=(0, 0))
    assert (rows[0]["m5_status"], rows[0]["m5_detail"], rows[0]["rebalanced"]) == (
        "SOLVER_FAILED", "synthetic", 0)
    assert snap.requests["A"].accepted_at == 0 and snap.requests["A"].completed_at == 17


def test_independent_validation_once_per_completed_debug_batch(tmp_path, monkeypatch):
    from src.m4.validation import ScheduleValidator
    calls = []
    validate = ScheduleValidator.validate

    def counted(self, schedule):
        calls.append(schedule.time)
        return validate(self, schedule)

    monkeypatch.setattr(ScheduleValidator, "validate", counted)
    _, rows, _, _ = scenario(tmp_path, [(0, "A", 0, 1)])
    assert calls == [row["batch_time"] for row in rows]
    assert sum(r["candidates_evaluated"] for r in rows) > len(calls)
    scenario(tmp_path, [(0, "A", 0, 1)], validator=False)
    assert len(calls) == len(rows)


def test_solver_elapsed_time_does_not_change_the_event_clock(tmp_path, monkeypatch):
    from time import sleep
    from src.m4.halns import M4HALNS
    solve = M4HALNS.solve

    def slow(self, *args, **kwargs):
        sleep(.02)
        return solve(self, *args, **kwargs)

    monkeypatch.setattr(M4HALNS, "solve", slow)
    snap, rows, _, _ = scenario(tmp_path, [(0, "A", 0, 1)])
    assert all(r["batch_wall_ms"] > 15 for r in rows)
    assert (snap.requests["A"].accepted_at, snap.requests["A"].completed_at) == (0, 17)


def test_shared_future_flight_has_one_departure_and_landing(tmp_path):
    snap, rows, events, _ = scenario(tmp_path, [(159, "A", 0, 1), (160, "B", 0, 1)], cars=(),
                                     flights=True, evaluation=(0, 165))
    flight = snap.flights["0>1:1"]
    assert flight.reservations == (("A", 1), ("B", 1))
    assert all((r.accepted_at, r.first_pickup_at, r.completed_at, r.plan.service_mode) ==
               (160, 180, 185, "RS-UAM") for r in snap.requests.values())
    assert [(e.kind.value, e.time) for e in events if e.object_id == flight.flight_id] == [
        ("DEPART", 180), ("LAND", 185)]
    assert sum(r["accepted_new"] for r in rows) == rows[-1]["evaluation_completed"] == 2


def test_end_to_end_leading_walk_flight_and_trailing_walk(tmp_path):
    net = network([0, .0001, .01, .0101], [(0, 1, 40), (1, 2, 450), (2, 3, 40)],
                  (1, 2), [(1, 2, 5)])
    final, rows, events, _ = scenario(tmp_path, [(157, "W", 0, 3)], cars=(), net=net,
                                      evaluation=(0, 165), coverage=1000)
    passenger = final.requests["W"]
    assert (passenger.accepted_at, passenger.first_pickup_at, passenger.completed_at) == (160, 180, 193)
    assert [(e.kind.value, e.time) for e in events if e.kind in ("WALK_START", "WALK_END")] == [
        ("WALK_START", 160), ("WALK_END", 168), ("WALK_START", 185), ("WALK_END", 193)]
    assert rows[-1]["evaluation_completed"] == 1


def test_reports_solver_calls_and_cache_storage_separately(tmp_path):
    _, rows, _, _ = scenario(tmp_path, [(3, "R", 0, 1)])
    for row in rows:
        assert row["batch_wall_ms"] >= row["m4_call_wall_ms"] >= row["m4_wall_ms"]
        assert row["decode_count"] >= 1 + row["candidates_evaluated"]
        assert row["request_table_bytes"] > 0 and row["event_buffer_bytes"] > 0


def test_lost_edge_event_fails_at_next_boundary(tmp_path, monkeypatch):
    # Suppress the physical event after the car starts driving; the normal
    # clock advances at most once past its due tick before reporting the fault.
    original = Simulator._schedule
    def lose_edge(self, tick, kind, *args, **kwargs):
        if kind != "EDGE_END":
            return original(self, tick, kind, *args, **kwargs)
    monkeypatch.setattr(Simulator, "_schedule", lose_edge)
    advance = Simulator.advance_to
    def bounded(self, when):
        if when > 40:
            pytest.fail("missing motion was not diagnosed within four synthetic batches")
        return advance(self, when)
    monkeypatch.setattr(Simulator, "advance_to", bounded)
    with pytest.raises(ValueError, match="overdue road edge"):
        scenario(tmp_path, [(0, "B", 1, 0)], cars=(0,), road_time=17, evaluation=(0, 15))


def test_unreachable_m5_transport_records_failure_and_preserves_m4(tmp_path):
    net = network([0, .01, .03, .04], [(0, 1, 17), (1, 0, 17), (2, 3, 17), (3, 2, 17)])
    snap, rows, _, _ = scenario(tmp_path, [(0, "A", 0, 1), (0, "B", 2, 3)], cars=(0, 0), net=net)
    assert rows[0]["m5_status"] == "UNEXECUTABLE" and rows[0]["m5_route_failure"]["reason"] == "NO_DIRECTED_PATH"
    assert rows[0]["m5_uncovered"] == 1 and rows[0]["rebalanced"] == 0
    assert snap.requests["A"].completed_at == 17 and snap.requests["B"].status == "EXPIRED_UNSERVED"
