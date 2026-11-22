import json
from hashlib import sha256
from pathlib import Path
from random import Random

import pytest

from src.cli.main import main
from src.common.config import load_config
from src.m3 import M3Searcher
from src.m4.halns import M4Config, M4HALNS
from tests.p4_examples import leg, release, setup


def test_profile_inherits_only_observation_settings():
    base = load_config("configs/debug_10min.yaml")
    diagnostic = load_config("configs/debug_10min_profile.yaml")
    assert diagnostic.pop("profiling")["enabled"] is True
    assert diagnostic == base


def test_exclusive_timing_counts_only_direct_children():
    from src.evaluation.profiling import BatchProfile, ProfileConfig
    clock = iter((0, 2, 3, 8, 9, 13))
    profile = BatchProfile(ProfileConfig(enabled=True), 1, clock=lambda: next(clock))
    with profile.span("outer"):
        with profile.span("middle"):
            with profile.span("inner"):
                pass
    assert [profile.stages[k]["exclusive_ns"] for k in ("outer", "middle", "inner")] == [6, 2, 5]
    assert profile.stages["outer"]["inclusive_ns"] == 13


@pytest.mark.parametrize("deadline", [False, True])
def test_profiling_preserves_rng_work_best_and_snapshot(deadline):
    from src.evaluation.profiling import BatchProfile, ProfileConfig
    sim = setup([(0, 1, 1), (1, 0, 2)], [("car", 0, 3)])
    catalog = {r: (release(sim, r, [leg("ground", 0, 1, 1)], size=2),) for r in ("A", "B")}
    snapshot = sim.batch_snapshot()
    sparse = M3Searcher(sim.router).search(snapshot, catalog)
    config = M4Config(validate_final=True)
    profile = BatchProfile(ProfileConfig(enabled=True, candidate_every=1), 1)
    profile.select_requests(snapshot, catalog, sparse)
    results = []
    for observer in (None, profile):
        ticks = iter(range(100000))
        rng = Random(38)
        clock = (lambda: next(ticks) / 40) if deadline else (lambda: 0.)
        result = M4HALNS(sim.router, config).solve(snapshot, catalog, sparse, rng=rng,
                    wall_clock=clock, cpu_clock=lambda: 0., profile=observer)
        results.append((result.stats, result.state, result.best_solution.encoding,
                        result.best_solution.tasks, result.best_solution.v, rng.getstate()))
        assert result.timing.timed_out == deadline
        assert result.best_solution.feasible and result.best_solution.v >= result.safe_baseline.v
    assert results[0] == results[1]
    assert sim.batch_snapshot() == snapshot
    assert profile.requests and any(r["attempts"] for r in profile.requests.values())
    assert {"s0_prepare", "s0_decode", "validator"} <= profile.stages.keys()


@pytest.mark.parametrize("cap", [64, 100000])
def test_micro_profile_is_bounded_and_checksummed(tmp_path, cap):
    base = Path("configs/micro.yaml").resolve().as_posix()
    config = tmp_path / "micro_profile.yaml"
    config.write_text(f"extends: {base}\nprofiling:\n  enabled: true\n  batch_every: 1\n"
                      f"  candidate_every: 1\n  max_requests_per_batch: 1\n  max_output_bytes: {cap}\n",
                      encoding="utf-8")
    runs = tmp_path / "out"
    assert main(["simulate", "--config", str(config), "--output-dir", str(runs)]) == 0
    run, = runs.iterdir()
    m = json.loads((run / "manifest.json").read_text())
    data = (run / "profiling.jsonl").read_bytes()
    assert m["run_class"] == "PROFILING" and m["status"] == "COMPLETE"
    assert len(data) <= cap and m["output_checksums"]["profiling.jsonl"]["sha256"] == sha256(data).hexdigest()
    rows = [json.loads(s) for s in data.splitlines()]
    assert all(len(r["requests"]) <= 1 for r in rows)
    if cap == 64:
        assert not rows and m["profiling_counts"]["batches_truncated"] > 0
    else:
        assert rows and any(r["requests"] for r in rows)
        assert all("passenger_commit" in r["stages"] for r in rows)
        assert all("final_state" in q and "outcome" in q for r in rows for q in r["requests"])


@pytest.mark.parametrize("full", [False, True])
def test_sampled_legs_conflict_repair_and_timing_cap(full):
    from src.evaluation.profiling import BatchProfile, ProfileConfig
    from tests.performance.p6_profiling_synthetic import fixture
    sim, snap, plans, sparse = fixture(10)
    opts = ProfileConfig(enabled=True, candidate_every=1, max_candidates_per_batch=2,
                         max_detail_spans_per_batch=3, max_requests_per_batch=3)
    profile = BatchProfile(opts, 1)
    profile.select_requests(snap, plans, sparse)
    other = BatchProfile(opts, 1)
    other.select_requests(snap, dict(reversed(list(plans.items()))), sparse)
    assert list(other.requests) == list(profile.requests)
    result = M4HALNS(sim.router, M4Config(noD=full, inner_loops=1)).solve(
        snap, plans, sparse, rng=Random(101), wall_clock=lambda: 0., profile=profile)
    row = profile.record(snap, result)
    assert row["detail_spans"] == 3 and row["detail_spans_truncated"] > 0
    assert row["sampled_candidates"] == 2 and row["candidate_samples_truncated"] > 0
    assert row["requests_truncated"] == 5
    assert sum(row["operator_calls"].values()) == row["candidates_started"] - 1
    for req in row["requests"]:
        assert req["has_binding_options"] and req["plans"][0]["legs"][0]["count"] == 1
        assert req["attempts"] == req["completed_attempts"] > 0 and req["retained_feasible"] == 0
        assert req["first_conflict"] == "pickup_window" and req["outcome"] == "removed_in_observed_trials"


def test_type3_replay_preserves_physical_order_with_profiling():
    from src.evaluation.profiling import BatchProfile, ProfileConfig
    from src.simulation.transactions import commit_passengers
    from tests.integration.test_p6_m4_commit import shared, solve
    def replay(enabled):
        sim, plans = shared()
        states = []
        for batch, tick in enumerate((0, 2), 1):
            sim.advance_to(tick)
            profile = BatchProfile(ProfileConfig(enabled=True), batch) if enabled else None
            catalog = plans if batch == 1 else {}
            snap, result = solve(sim, catalog, profile=profile, wall_clock=lambda: 0.)
            states.append((result.best_solution.encoding, result.state))
            commit_passengers(sim, snap, catalog, result)
        sim.advance_to(15)
        return states, sim.snapshot().requests, sim.history
    states, requests, history = replay(True)
    assert [s[0].sequence for s in states] == [tuple("BBAA"), tuple("BAA")]
    assert (states, requests, history) == replay(False)


def test_profiled_timeout_keeps_completed_improvement(monkeypatch):
    from src.evaluation.profiling import BatchProfile, ProfileConfig
    from src.m4.partitioned_decoder import PartitionedDecoder
    from tests.integration.test_p4b_search import solve
    original = PartitionedDecoder.decode
    now, calls = [0.], []
    def slow_second(self, encoding, **kw):
        calls.append(encoding)
        if len(calls) == 2:
            now[0] = 12.
        return original(self, encoding, **kw)
    monkeypatch.setattr(PartitionedDecoder, "decode", slow_second)
    sim = setup([(0, 1, 3)], [("v", 0, 4)])
    catalog = {"A": (release(sim, "A", [leg("c", 0, 1, 3)]),)}
    out = solve(sim, catalog, profile=BatchProfile(ProfileConfig(enabled=True), 1),
                wall_clock=lambda: now[0], cpu_clock=lambda: 0.)
    assert out.timing.timed_out and out.stats.candidates_started == 2 and out.stats.candidates_completed == 1
    assert out.best_solution.completion == {"A": 3} and out.safe_baseline.completion == {}


def test_profiling_observes_failed_m5_without_publishing(tmp_path):
    from src.evaluation.profiling import ProfileConfig, ProfileRun
    from tests.integration.test_p6_rolling import network, scenario
    net = network([0, .01, .03, .04], [(0, 1, 17), (1, 0, 17), (2, 3, 17), (3, 2, 17)])
    args = [(0, "A", 0, 1), (0, "B", 2, 3)]
    prof = ProfileRun(tmp_path, ProfileConfig(enabled=True, batch_every=2))
    snap, _, events, _ = scenario(tmp_path, args, cars=(0, 0), net=net, profile_run=prof)
    ref, _, reference_events, _ = scenario(tmp_path, args, cars=(0, 0), net=net)
    assert events == reference_events and snap.requests == ref.requests
    samples = [json.loads(s) for s in prof.path.read_bytes().splitlines()]
    assert all(s["batch"] % 2 == 1 for s in samples)
    first = samples[0]
    assert first["m5"]["reason"] == "NO_DIRECTED_PATH"
    assert first["m5"]["cross_quota"] == 1 and first["m5"]["published"] == 0
    assert first["stages"]["rebalance_commit"]["calls"] == first["stages"]["validator"]["calls"] == 1
    assert {r["request_id"]: r["outcome"] for r in first["requests"]} == {
        "A": "accepted", "B": "no_complete_binding_options"}


def test_default_contract_excludes_profile_file_metadata_and_columns(tmp_path):
    assert main(["simulate", "--config", "configs/micro.yaml", "--output-dir", str(tmp_path)]) == 0
    run, = tmp_path.iterdir()
    manifest = json.loads((run / "manifest.json").read_text())
    assert not (run / "profiling.jsonl").exists()
    assert "run_class" not in manifest and "profiling_counts" not in manifest
    with (run / "batches.csv").open() as stream:
        assert "profil" not in stream.readline()
    assert "run_class" not in json.loads((run / "summary.json").read_text())


@pytest.mark.parametrize("options", [{"enabled": "false"}, {"candidate_every": 0}, {"bad_key": 2},
                                     {"max_requests_per_batch": 65}, {"max_output_bytes": -1}])
def test_invalid_profile_configuration(options):
    from src.evaluation.profiling import ProfileConfig
    with pytest.raises(ValueError):
        ProfileConfig.from_config({"profiling": options})



def test_validator_failure_in_profiling_run_is_preserved(tmp_path, monkeypatch):
    from src.m4.validation import ScheduleValidator
    cfg = tmp_path / "profile.yaml"
    cfg.write_text(f"extends: {Path('configs/micro.yaml').resolve().as_posix()}\n"
                   "profiling:\n  enabled: true\n", encoding="utf-8")
    def reject(self, candidate):
        raise ValueError("injected independent validation failure")
    monkeypatch.setattr(ScheduleValidator, "validate", reject)
    output = tmp_path / "out"
    assert main(["simulate", "--config", str(cfg), "--output-dir", str(output)]) == 1
    run, = output.iterdir()
    manifest = json.loads((run / "manifest.json").read_text())
    failure = json.loads((run / "failure.json").read_text())
    assert manifest["run_class"] == "PROFILING" and manifest["status"] == "FAILED"
    assert failure["batch"] == 1 and "injected independent validation failure" in failure["traceback"]
    assert all(r["accepted_at"] is None for r in failure["snapshot"]["requests"].values())
    assert not (run / "summary.json").exists()

