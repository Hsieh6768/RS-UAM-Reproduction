import csv
import json
from datetime import datetime

import pytest

from src.cli.main import main


@pytest.mark.parametrize("profile,validator,seconds", [
    ("debug_10min", True, 600), ("debug_30min", True, 1800),
    ("sensitivity_5h", False, 18000), ("paper_main", False, 172800),
    ("cpu_budget_diagnostic", True, 1800),
])
def test_manual_profiles_can_be_inspected_without_simulation(profile, validator, seconds, capsys, monkeypatch):
    from src.network.prepared import PreparedNetwork
    monkeypatch.setattr(PreparedNetwork, "from_config", lambda *a, **k: pytest.fail("loaded live network"))
    def metadata(config, *, verify):
        assert verify is False
        return [], datetime(2013, 5, 8), {"input_counts": {"WARMUP": 2490, "EVALUATION": 33565, "TAIL": 18055}}
    monkeypatch.setattr("src.cli.experiment.official_inputs", metadata)
    assert main(["simulate", "--config", f"configs/{profile}.yaml", "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["validator_enabled"] is validator
    assert result["evaluation_seconds"] == seconds and result["warmup_seconds"] == 7200
    assert result["input_counts"] == {"WARMUP": 2490, "EVALUATION": 33565, "TAIL": 18055}


def test_cli_micro_run_repeatable_with_unique_outputs(tmp_path):
    from src.network.prepared import PreparedNetwork
    from unittest.mock import patch
    args = ["simulate", "--config", "configs/micro.yaml", "--output-dir", str(tmp_path)]
    with patch.object(PreparedNetwork, "from_config", side_effect=AssertionError("real network")):
        assert main(args) == main(args) == 0
    runs = sorted(tmp_path.iterdir())
    assert len(runs) == 2
    a, b = [(p / "requests.csv").read_bytes() for p in runs]
    assert a == b
    with (runs[0] / "requests.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert any(r["state"] == "COMPLETED" and r["cohort"] == "EVALUATION" for r in rows)
    assert (runs[0] / "vehicle_events.csv.gz").stat().st_size > 0
    manifest = json.loads((runs[0] / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["initial_fleet_nodes"]) == 2 and len(manifest["synthetic_demand_sha256"]) == 64


def test_cli_failure_returns_nonzero_and_preserves_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("src.simulation.rolling.run_rolling",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected M4 failure")))
    assert main(["simulate", "--config", "configs/micro.yaml", "--output-dir", str(tmp_path)]) != 0
    run, = tmp_path.iterdir()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "FAILED" and "injected M4 failure" in (run / "console.log").read_text()


def test_final_audit_failure_identifies_last_real_batch(tmp_path, monkeypatch):
    from src.evaluation.outputs import RunOutput
    def reject(self, snap):
        raise ValueError("synthetic final audit failure")
    monkeypatch.setattr(RunOutput, "complete", reject)
    assert main(["simulate", "--config", "configs/micro.yaml", "--output-dir", str(tmp_path)]) == 1
    run, = tmp_path.iterdir()
    failure = json.loads((run / "failure.json").read_text(encoding="utf-8"))
    assert failure["phase"] == "final_audit" and failure["batch"] == failure["last_successful_batch"]
    assert failure["snapshot"]["requests"]["eval"]["status"] == "COMPLETED"


def test_sample_envelopes_use_original_seconds_and_load_direct_time_only_on_release(tmp_path):
    from src.data.experiment import sample_envelopes
    from tests.p5_examples import world
    path = tmp_path / "evaluation.csv"
    path.write_text("request_id,request_time_local,origin_node,destination_node,passenger_count,"
                    "pickup_longitude,pickup_latitude,dropoff_longitude,dropoff_latitude\n"
                    "7,2013-05-05 00:10:01,1,2,2,-74,40.7,-73.9,40.71\n", encoding="utf-8")
    sim = world([(1, -74, 40.7), (2, -73.9, 40.71)], [(1, 2, 5)])
    at, rid, load = next(sample_envelopes([path], datetime(2013, 5, 4, 22), sim.router, 420, 420))
    assert (at, rid) == (7801, "7")
    assert load().direct_road_time_s == 5 and load().submitted_at == at


def test_fleet_initialization_is_nested():
    from src.data.experiment import place_fleet
    from tests.p5_examples import world
    sims = [world([(1, -74, 40), (2, -73, 41)], [(1, 2, 5), (2, 1, 6)]) for _ in range(2)]
    for sim, size in zip(sims, [50, 150]):
        place_fleet(sim, size, 101, 4)
    small, large = [s.batch_snapshot().vehicles for s in sims]
    assert len(small) == 50 and len(large) == 150
    assert all(large[vid] == car for vid, car in small.items())


@pytest.mark.parametrize("variant,section,key,value", [
    ("rs", "requests", "allowed_modes", ["car"]), ("OP", "m1", "top_k", 1),
    ("noR", "m5", "enabled", False), ("noS", "m4", "simulated_annealing", False),
    ("noD", "m4", "decoder", "full"), ("noA", "m4", "dynamic_operator_scoring", False),
])
def test_cli_effective_overrides_in_dry_run(variant, section, key, value, capsys):
    assert main(["simulate", "--config", "configs/micro.yaml", "--dry-run", "--variant", variant,
                 "--no-validator", "--budget-seconds", "30", "--seed", "77"]) == 0
    resolved = json.loads(capsys.readouterr().out)["config"]
    assert resolved[section][key] == value
    assert resolved["m4"]["seed"] == 77 and resolved["m4"]["wall_budget_seconds"] == 30
    assert resolved["validation"]["schedule_validator_enabled"] is False

