"""P6 output checks use executed P1 events as independently readable evidence."""
import csv
import gzip
import json

import pytest
import yaml

from src.m2.passengers import FlightInstance, Leg, Plan, Request
from tests.integration.test_p1_engine import world


CONFIG = {
    "simulation": {"warmup_start": "2013-05-04T22:00:00", "evaluation_start": "2013-05-04T22:00:00",
                   "evaluation_end_exclusive": "2013-05-04T22:00:10"},
    "m4": {"wall_budget_seconds": 10, "budget_clock": "wall"},
}


def ledger(tmp_path):
    from src.evaluation.outputs import RunOutput
    return RunOutput(tmp_path, CONFIG, {"seed_halns": 101})


def fixed_flight():
    sim = world([(0, 1, 130)])
    sim.add_flight(FlightInstance("F", 0, 1, 2, 10, 4))
    for rid, size in (("A", 1), ("B", 3)):
        sim.schedule_arrival(rid, 0, lambda r=rid, n=size: Request(r, 0, 0, 1, n, 130))
        sim.accept(rid, Plan("p" + rid, rid, (Leg("air", "FLIGHT", 0, 1, 8),)), ("F",))
    return sim


def metrics(**kw):
    row = dict(batch=1, batch_time=0, cohort="EVALUATION", new_req=2, type1=2, type2=0, type3=0,
               accepted_new=2, expired=0, pending_after=0, accepted_incomplete=2,
               fleet_idle=1, fleet_loaded=0, fleet_pickup=0, fleet_rebalancing=0,
               evaluation_accepted=2, evaluation_completed=0,
               m1_wall_ms=1, m3_wall_ms=3, m4_wall_ms=100, m4_cpu_ms=80,
               m5_wall_ms=20, m5_cpu_ms=15, validator_wall_ms=4, batch_wall_ms=200,
               candidates_evaluated=2, budget_exhausted=False, m5_status="SKIPPED", rebalanced=0,
               m4_wall_budget_exceeded=False, batch_wall_budget_exceeded=False)
    return dict(row, **kw)


def test_p6_output_complete_shared_flight_signed_detour_and_exact_event_time(tmp_path, capsys):
    out = ledger(tmp_path)
    sim = fixed_flight()
    out.events(sim.history)
    out.batch(metrics())
    assert "2013-05-04 22:00:00" in capsys.readouterr().out
    assert "batch=1" in (out.path / "console.log").read_text(encoding="utf-8")
    sim.advance_to(10)
    out.events(sim.history[2:])
    out.batch(metrics(batch=2, batch_time=10, cohort="TAIL", new_req=0, type1=0,
                      accepted_new=0, accepted_incomplete=0, evaluation_completed=2,
                      m4_wall_ms=500, batch_wall_ms=15000, batch_wall_budget_exceeded=True))
    out.complete(sim.snapshot())

    def read_json(name):
        return json.loads((out.path / name).read_text(encoding="utf-8"))
    summary = read_json("summary.json")
    assert summary["service_rate"] == 1
    assert summary["accepted_evaluation"] == summary["completed_evaluation"] == 2
    assert summary["mean_detour_min"] == -2
    assert summary["service_modes"]["RS-UAM"] == 2
    assert summary["performance"]["m4_wall_ms"]["max"] == 500
    assert summary["performance"]["m5_status_counts"] == {"SKIPPED": 2}
    assert read_json("audit.json")["status"] == "PASS"
    assert read_json("performance.json")["m4_wall_ms"]["mean"] == 300
    assert read_json("performance.json")["batch_wall_budget_exceeded_rate"] == 0.5
    manifest = read_json("manifest.json")
    assert manifest["status"] == "COMPLETE" and manifest["seed_halns"] == 101
    for name in ("requests.csv", "batches.csv", "flight_usage.csv", "vehicle_events.csv.gz", "summary.md"):
        assert manifest["output_checksums"][name]["size_bytes"] == (out.path / name).stat().st_size
    assert yaml.safe_load((out.path / "config.yaml").read_text(encoding="utf-8")) == CONFIG
    with (out.path / "flight_usage.csv").open(encoding="utf-8") as handle:
        flight, = csv.DictReader(handle)
        assert flight["reserved_seats"] == "4"
        assert json.loads(flight["reservations"]) == {"A": 1, "B": 3}
    with gzip.open(out.path / "vehicle_events.csv.gz", "rt", encoding="utf-8") as handle:
        assert [(e["kind"], e["time"]) for e in csv.DictReader(handle)] == [
            ("ARRIVAL", "0"), ("ARRIVAL", "0"), ("DEPART", "2"), ("LAND", "10")]
    with (out.path / "requests.csv").open(encoding="utf-8") as handle:
        assert [row["request_id"] for row in csv.DictReader(handle)] == ["A", "B"]


@pytest.mark.parametrize("finish_without_events", [True, False])
def test_p6_output_audit_detects_missing_physical_history_or_unfinished_passenger(tmp_path, finish_without_events):
    out = ledger(tmp_path)
    sim = fixed_flight()
    if finish_without_events:
        sim.advance_to(10)
    out.events(sim.history[:2])
    out.batch(metrics(evaluation_completed=2 if finish_without_events else 0))
    with pytest.raises(ValueError) as error:
        out.complete(sim.snapshot())
    out.fail(error.value, sim.snapshot(), 2)
    manifest = json.loads((out.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "FAILED"
    assert not (out.path / "summary.json").exists()
    failure = json.loads((out.path / "failure.json").read_text(encoding="utf-8"))
    assert failure["batch"] == 2 and failure["snapshot"]["requests"]["A"]["plan"]["plan_id"] == "pA"
    assert "ValueError" in failure["traceback"] and failure["last_successful_batch"] == 1


def test_p6_output_same_seed_creates_independent_result_directories(tmp_path):
    first, second = ledger(tmp_path), ledger(tmp_path)
    assert first.path != second.path
    for out in (first, second):
        out.fail(KeyboardInterrupt(), None, None)
        assert json.loads((out.path / "manifest.json").read_text(encoding="utf-8"))["status"] == "INTERRUPTED"



def test_p6_audit_reconciles_fractional_road_replay_and_vehicle_mileage(tmp_path):
    from tests.integration.test_p1_engine import car_actions, proposal
    from src.simulation.events import Action
    out = ledger(tmp_path)
    sim = world([(0, 1, 2.5), (1, 2, 1.5)])
    sim.schedule_arrival("C", 0, lambda: Request("C", 0, 0, 2, 2, 4))
    sim.accept("C", Plan("p", "C", (Leg("car", "CAR", 0, 2, 4),)), ("v",))
    pickup, dropoff = car_actions("C", "car", 1, 5)
    sim.commit_route(proposal(sim, actions=(pickup, Action(1, "DRIVE", 2), dropoff)))
    sim.advance_to(10)
    out.events(sim.history)
    out.batch(metrics(batch_time=10, new_req=1, type1=1, accepted_new=1, accepted_incomplete=0,
                      evaluation_accepted=1, evaluation_completed=1))
    out.complete(sim.snapshot())
    audit = json.loads((out.path / "audit.json").read_text(encoding="utf-8"))
    assert float(audit["road_metres"]["LOADED"]) == pytest.approx(4 * 6.660896)
    assert audit["event_counts"]["EDGE_END"] == 2 and audit["task_count"] == 2
    with gzip.open(out.path / "vehicle_events.csv.gz", "rt", encoding="utf-8") as handle:
        edges = [e for e in csv.DictReader(handle) if e["kind"] == "EDGE_END"]
        assert [(e["time"], e["physical_time"]) for e in edges] == [("4", "3.5"), ("5", "5.0")]


def test_p6_performance_counts_operator_draws_and_cache_hits(tmp_path):
    out = ledger(tmp_path)
    sim = fixed_flight()
    sim.advance_to(10)
    out.events(sim.history)
    out.batch(metrics(batch_time=10, evaluation_completed=2, accepted_incomplete=0,
                      operator_draws={"cut": 3, "swap": 1}, m1_planned=2, m1_cache_hits=6, operator_rewards=2))
    out.complete(sim.snapshot())
    perf = json.loads((out.path / "performance.json").read_text(encoding="utf-8"))
    assert perf["operator_draws"] == {"cut": 3, "swap": 1}
    assert perf["m1_cache_hit_rate"] == 0.75
    assert perf["operator_rewards"]["total"] == 2
