from dataclasses import replace
import json

import pytest

from src.m1 import M1Planner
from src.m2.passengers import FlightInstance, WalkLink, task_id
from src.m3 import M3Searcher
from src.m4 import BaselineOrderRequired, Choice, Encoding, FullDecoder, ScheduleValidator, SnapshotError
from src.simulation.events import Action, CandidateRoute
from tests.p4_examples import batch, encoding, leg, release, setup


def test_four_seats_two_locked_two_new_parties_and_airborne_prefix():
    sim = setup([(0, 1, 10)], flights=[FlightInstance("f", 0, 1, 20, 25)])
    plans = [release(sim, rid, [leg("air", 0, 1, 5, "FLIGHT")], size=2) for rid in "HAB"]
    sim.accept("H", plans[0], ("f",))
    before = sim.batch_snapshot()
    decoder = batch(sim, *plans)
    for new in "AB":
        one = decoder.decode(encoding({rid: ("f",) for rid in ("H", new)}, new+"H"))
        assert one.flight_seats["f"] == {"H": 2, new: 2}
        assert one.f1 == 200 and one.f2 == pytest.approx(50 / 60)
        ScheduleValidator(decoder).validate(one)
    both = decoder.decode(encoding({rid: ("f",) for rid in "HAB"}, "AHB"))
    assert both.flight_seats["f"] == {"H": 2, "A": 2}
    assert [(r.request_id, r.reason) for r in both.repairs] == [("B", "flight_capacity")]
    with pytest.raises(TypeError):
        both.flight_seats["f"]["B"] = 2
    assert sim.batch_snapshot() == before
    sim.advance_to(21)
    airborne = batch(sim, *plans).safe_baseline
    assert airborne.encoding.sequence == () and airborne.completion == {"H": 25}
    assert airborne.trips["H"][0].pickup == 20
    with pytest.raises(ValueError):
        both.better_than(airborne)


def test_in_motion_full_car_and_type3_invalidity():
    sim = setup([(0, 1, 20), (1, 2, 100), (2, 1, 100)], [("v", 0, 4)])
    old = release(sim, "H", [leg("h", 0, 1, 20, ld=40)], size=4)
    new = release(sim, "A", [leg("a", 1, 2, 100)], size=2)
    sim.accept("H", old, ("v",))
    pick, drop = (task_id("H", "h", k) for k in ("PICKUP", "DROPOFF"))
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "v", actions=(
        Action(0, "PICKUP", pick), Action(0, "DRIVE", 1), Action(22, "DROPOFF", drop))))
    sim.advance_to(10)
    before, history = sim.batch_snapshot(), sim.history
    decoder = batch(sim, new)
    base = decoder.safe_baseline
    assert base.encoding.sequence == ("H",) and base.completion["H"] == 22
    assert base.trips["H"][0].pickup == 0
    assert decoder.decode(base.encoding, preserve_appointments=True) == base
    ScheduleValidator(decoder).validate(base)
    choices = {"H": ("v",), "A": ("v",)}
    good = decoder.decode(encoding(choices, "HAA"))
    assert [(s.at, s.onboard_after) for s in good.vehicles["v"]] == [(20, 0), (20, 2), (120, 0)]
    bad = decoder.decode(encoding(choices, "AHA"))
    assert [r.reason for r in bad.repairs] == ["vehicle_capacity"]
    for candidate in [encoding({"A": ("v",)}, "AA"), encoding({"H": ("other",)}, "H")]:
        invalid = decoder.decode(candidate)
        assert not invalid.feasible and not invalid.repairs and invalid.v is None
    assert sim.batch_snapshot() == before and sim.history == history


def test_candidate_that_delays_locked_drop_is_invalid_without_removing_new_party():
    sim = setup([(0, 1, 2), (1, 2, 20), (2, 1, 20)], [("v", 0, 4)])
    h = release(sim, "H", [leg("h", 0, 1, 2, ld=4)])
    a = release(sim, "A", [leg("a", 1, 2, 20)])
    sim.accept("H", h, ("v",))
    decoder = batch(sim, a)
    result = decoder.decode(encoding({"H": ("v",), "A": ("v",)}, "HAAH"))
    assert not result.feasible and result.failure.request_id == "H"
    assert result.failure.reason == "dropoff_window" and not result.repairs
    assert decoder.safe_baseline.completion == {"H": 2}


def test_active_trailing_walk_and_snapshot_corruption(tmp_path):
    sim = setup([(0, 1, 20), (1, 2, 10)], flights=[FlightInstance("f", 0, 1, 5, 10)])
    h = release(sim, "H", [leg("air", 0, 1, 5, "FLIGHT"), WalkLink("end", 1, 2, 3, 1)])
    sim.accept("H", h, ("f", None))
    sim.advance_to(11)
    before = sim.batch_snapshot()
    decoder = batch(sim)
    assert decoder.safe_baseline.completion == {"H": 13}
    assert decoder.safe_baseline.trips["H"][1].pickup == 10
    assert decoder.safe_baseline.encoding.sequence == ()
    ScheduleValidator(decoder).validate(decoder.safe_baseline)
    corrupt = replace(before, flights={"f": replace(before.flights["f"], reservations=())})
    with pytest.raises(SnapshotError):
        FullDecoder(sim.router, corrupt, {}, M3Searcher(sim.router).search(corrupt, {}), failure_dir=tmp_path)
    evidence = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert evidence["status"] == "FAILED" and evidence["snapshot"]["time"] == 11
    assert "flight commitment" in evidence["cause"]


def test_baseline_appointment_drive_and_physical_replay():
    sim = setup([(0, 1, 4), (1, 2, 7)], [("v", 0, 4)])
    h = release(sim, "H", [leg("h", 1, 2, 7)])
    sim.accept("H", h, ("v",))
    pick, drop = (task_id("H", "h", k) for k in ("PICKUP", "DROPOFF"))
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "v", actions=(
        Action(10, "DRIVE", 1), Action(20, "PICKUP", pick),
        Action(25, "DRIVE", 2), Action(40, "DROPOFF", drop))))
    decoder = batch(sim)
    safe = decoder.safe_baseline
    assert [(s.arrival, s.at) for s in safe.vehicles["v"]] == [(14, 20), (32, 40)]
    ScheduleValidator(decoder).validate(safe)
    sim.advance_to(40)
    assert sim.batch_snapshot().requests["H"].completed_at == safe.completion["H"] == 40


def test_real_m1_m3_type2_eligible_again_after_candidate_removal():
    from tests.p2_examples import network
    from src.m2.passengers import Request
    from src.simulation.engine import Simulator
    net = network([0, .005, .01], [(0, 1, 35), (2, 1, 5)])
    sim = Simulator(net.road)
    sim.add_vehicle("reachable", 0)
    sim.add_vehicle("stranded", 2)
    req = Request("R", 0, 0, 1, 2, 35)
    sim.schedule_arrival("R", 0, lambda: req)
    sim.advance_to(10)
    snap = sim.batch_snapshot()
    assert snap.request_types(0)["R"] == 2
    plans = {"R": M1Planner().generate(req, net)}
    sparse = M3Searcher(net.road).search(snap, plans)
    decoder = FullDecoder(net.road, snap, plans, sparse)
    pid = plans["R"][0].plan_id
    def try_car(vid):
        return decoder.decode(Encoding({"R": Choice(pid, (vid,))}, ("R", "R")))
    assert try_car("stranded").repairs[0].reason == "unreachable_road"
    recovered = try_car("reachable")
    assert recovered.completion == {"R": 45} and recovered.f2 == .75
    ScheduleValidator(decoder).validate(recovered)
    sim.advance_to(20)
    snap = sim.batch_snapshot()
    later = FullDecoder(net.road, snap, plans, M3Searcher(net.road).search(snap, plans))
    rescored = later.decode(recovered.encoding)
    assert rescored.completion == {"R": 55}
    with pytest.raises(ValueError):
        recovered.better_than(rescored)


def test_safe_unbooked_car_tasks_can_serve_two_full_groups_in_sequence():
    sim = setup([(0, 1, 2), (1, 0, 2)], [("v", 0, 4)])
    plans = [release(sim, rid, [leg("c", 0, 1, 2)], size=4) for rid in "AB"]
    for p in plans:
        sim.accept(p.request_id, p, ("v",))
    decoder = batch(sim)
    assert decoder.safe_baseline.completion == {"A": 2, "B": 6}
    ScheduleValidator(decoder).validate(decoder.safe_baseline)


def test_partial_p1_appointments_can_use_prior_remaining_sequence(tmp_path):
    sim = setup([(0, 1, 5), (1, 0, 5)], [("v", 0, 4)])
    a = release(sim, "A", [leg("a", 0, 1, 5)], size=4)
    b = release(sim, "B", [leg("b", 0, 1, 5, lp=2)], size=4)
    for plan in (a, b):
        sim.accept(plan.request_id, plan, ("v",))
    with pytest.raises(BaselineOrderRequired):
        batch(sim, failure_dir=tmp_path)
    dec = batch(sim, remaining_sequence="BBAA")
    assert dec.safe_baseline.completion == {"B": 5, "A": 15}
    ScheduleValidator(dec).validate(dec.safe_baseline)
