"""P6 manual experiment entry point, shared by micro and official demand."""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

from src.common.config import load_config
from src.data.experiment import inspect_sample_counts, official_inputs, place_fleet, sample_envelopes
from src.data.micro import COVERAGE_SECONDS, DEMAND, micro_envelopes, micro_network
from src.evaluation.profiling import ProfileConfig
from src.m1.config import M1Config
from src.m3.config import M3Config
from src.m4.halns import M4Config
from src.m5 import M5Config
from src.network.flights import AirNetwork
from src.network.prepared import PreparedNetwork
from src.simulation.engine import Simulator

VARIANTS = ("rs_uam", "rs", "noD", "noA", "noS", "OP", "noR")


def add_simulate(commands):
    parser = commands.add_parser("simulate", help="Run one configured rolling experiment")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Inspect metadata only, without creating a run")
    parser.add_argument("--fleet-size", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--variant", choices=VARIANTS)
    parser.add_argument("--output-dir", type=Path, help="Parent for unique run directories")
    parser.add_argument("--validator", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--m5", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--budget-clock", choices=("wall", "cpu"))
    parser.add_argument("--budget-seconds", type=int, choices=(10, 30))
    parser.add_argument("--wall-safety-cap", type=float, help="Wall safety cap for a CPU diagnostic")


def resolve(args):
    config = load_config(args.config)
    experiment = config["experiment"]
    for key, raw in (("fleet_size", args.fleet_size), ("seed", args.seed), ("variant", args.variant)):
        experiment[key] = raw if raw is not None else experiment.get(key, experiment["default_" + key])
    if "NOT_READY" in experiment["name"] or experiment["variant"] not in VARIANTS:
        raise ValueError("Unsupported experiment profile or variant")
    if type(experiment["fleet_size"]) is not int or experiment["fleet_size"] < 1 or type(experiment["seed"]) is not int:
        raise ValueError("Expected a positive fleet size and an integral seed")
    source = experiment.setdefault("source", "official")
    if source not in ("official", "synthetic"):
        raise ValueError("Unknown experiment source")
    variant = experiment["variant"]
    m4 = config["m4"]
    m4["seed"] = experiment["seed"]
    if variant == "rs":
        config["requests"].update(allowed_modes=["car"], max_transfers=0, max_service_legs=1)
    if variant == "OP":
        config["m1"]["top_k"] = 1
    if variant == "noD":
        m4["decoder"] = "full"
    if variant == "noS":
        m4["simulated_annealing"] = False
    if variant == "noA":
        m4["dynamic_operator_scoring"] = False
    if args.m5 is not None:
        config["m5"]["enabled"] = args.m5
    if variant == "noR":
        if args.m5:
            raise ValueError("--m5 conflicts with noR")
        config["m5"]["enabled"] = False
    if args.validator is not None:
        config["validation"]["schedule_validator_enabled"] = args.validator
    if args.output_dir:
        config["reporting"]["output_root"] = str(args.output_dir)
    if args.budget_clock:
        m4["budget_clock"] = args.budget_clock
    if args.budget_seconds:
        m4["wall_budget_seconds"] = args.budget_seconds
        if m4["budget_clock"] == "cpu":
            m4["cpu_budget_seconds"] = args.budget_seconds
    if args.wall_safety_cap is not None:
        m4["diagnostic_wall_safety_cap_seconds"] = args.wall_safety_cap
    if m4["budget_clock"] not in ("cpu", "wall") or m4["wall_budget_seconds"] not in (10, 30):
        raise ValueError("Expected a wall/CPU budget and a 10s or 30s paper reference")
    if m4["budget_clock"] == "cpu" and (m4.get("cpu_budget_seconds") not in (10, 30)
            or not 0 < m4.get("diagnostic_wall_safety_cap_seconds", 0) < float("inf")):
        raise ValueError("CPU diagnostics require a 10/30s CPU budget and a positive finite wall safety cap")
    sim = config["simulation"]
    if (config["data"]["timezone"] != "America/New_York" or sim["decision_latency_model"] != "instantaneous"
            or not sim["tail_competition_until_all_evaluation_terminal"] or sim["tail_requests_evaluated"]
            or not sim["finish_committed_after_tail"] or not config["validation"]["runtime_invariants_enabled"]
            or not config["validation"]["fail_fast_on_violation"]):
        raise ValueError("Configuration conflicts with the D004/D012/D014 experiment protocol")
    return config


def simulate(args):
    config = resolve(args)
    profile_config = ProfileConfig.from_config(config)
    if profile_config.enabled:
        config["profiling"] = asdict(profile_config)
    experiment, settings = config["experiment"], config["simulation"]
    origin, start, end = (datetime.fromisoformat(settings[k])
                          for k in ("warmup_start", "evaluation_start", "evaluation_end_exclusive"))
    if not origin <= start < end or any(t.microsecond or t.tzinfo for t in (origin, start, end)):
        raise ValueError("Invalid local whole-second experiment window")
    interval = settings["batch_seconds"]
    if type(interval) is not int or interval <= 0:
        raise ValueError("Batch interval must be a positive integer")
    tiny = experiment["source"] == "synthetic"
    if not tiny and origin < datetime(2013, 5, 4, 22):
        raise ValueError("Requested warmup starts before P0 sample coverage")
    modules = dict(m1_config=M1Config.from_config(config), m3_config=M3Config.from_config(config),
                   m4_config=M4Config.from_config(config), m5_config=M5Config.from_config(config))
    seed = experiment["seed"]
    eval_start = int((start - origin).total_seconds())
    eval_end = int((end - origin).total_seconds())
    meta = dict(mode=experiment["variant"], source=experiment["source"], vehicle_count=experiment["fleet_size"],
                seed_fleet=seed, seed_halns=seed, warmup_seconds=eval_start, evaluation_seconds=eval_end-eval_start,
                warmup_window=[origin.isoformat(), start.isoformat()], cohort_window=[start.isoformat(), end.isoformat()],
                tail_policy="compete_until_evaluation_terminal_then_drain",
                m4_wall_budget_seconds=config["m4"]["wall_budget_seconds"],
                validator_enabled=config["validation"]["schedule_validator_enabled"],
                method_deviations=["D002", "D007", "D011", "D012", "D014"])
    report = None
    if not tiny:
        meta["dataset_variant"] = config["inputs"].get("dataset_variant", "full")
    if profile_config.enabled:
        meta["run_class"] = "PROFILING"
    sim = None
    phase = "initialization"
    try:
        if not args.dry_run:
            from src.evaluation.outputs import RunOutput
            report = RunOutput(config["reporting"]["output_root"], config, meta)
            label = "PROFILING " if profile_config.enabled else ""
            print(f"{label}Run directory: {report.path.resolve()}", flush=True)
        if tiny:
            coverage_end = COVERAGE_SECONDS
            meta.update(input_counts={"synthetic": len(DEMAND)},
                synthetic_demand_sha256=sha256(json.dumps(DEMAND, separators=(",", ":")).encode()).hexdigest(),
                coverage_end_exclusive=(
                origin + timedelta(seconds=coverage_end)).isoformat(),
                expected_evaluation_requests=sum(eval_start <= at < eval_end for at, *_ in DEMAND))
        else:
            paths, coverage, input_meta = official_inputs(config, verify=not args.dry_run)
            coverage_end = int((coverage - origin).total_seconds())
            meta.update(input_meta)
            if input_meta.get("dataset_variant") == "scc100":
                meta["method_deviations"].append("D016")
        if eval_end > coverage_end:
            raise ValueError("Evaluation exceeds available demand coverage")
        if args.dry_run:
            print(json.dumps({**meta, "status": "DRY_RUN", "config": config}, indent=2))
            return 0
        if not tiny:
            meta["expected_evaluation_requests"] = inspect_sample_counts(paths, meta["input_counts"], start, end)
        report.update_manifest(meta)
        network = micro_network() if tiny else PreparedNetwork.from_config(config)
        req = config["requests"]
        tolerances = req["pickup_tolerance_seconds"], req["dropoff_tolerance_seconds"]
        arrivals = (micro_envelopes(network, *tolerances) if tiny else
                    sample_envelopes(paths, origin, network.road, *tolerances))
        sim = Simulator(network.road, batch_seconds=interval, failure_dir=report.path / "errors")
        place_fleet(sim, experiment["fleet_size"], seed, config["network"]["car_capacity"])
        report.update_manifest(dict(road_graph_version=network.road.version,
                               initial_fleet_nodes={k: v.node for k, v in sim.snapshot().vehicles.items()}))
        planning = network
        if experiment["variant"] == "rs":
            empty = MappingProxyType({})
            planning = PreparedNetwork(network.road, AirNetwork(empty, empty, network.air.phase_s, network.air.capacity))
        from src.simulation.rolling import run_rolling
        phase = "dispatch"
        final = run_rolling(sim, network, arrivals, eval_start=eval_start, eval_end=eval_end,
                            coverage_end=coverage_end, seed=seed, batch_seconds=interval, reporter=report,
                            planning_network=planning, failure_dir=report.path / "errors",
                            profile_run=report.profile_run, **modules)
        phase = "final_audit"
        report.complete(final)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if report is None:
            raise
        if sim is not None and phase != "final_audit":
            report.events(sim.drain_history())
        batch = report.last_batch.get("batch", 0) + int(phase == "dispatch")
        report.fail(exc, snapshot=sim.failure_snapshot() if sim is not None else None, batch=batch, phase=phase)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1

