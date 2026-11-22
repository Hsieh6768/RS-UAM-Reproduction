"""P6-NET exercises the production preparation, loader and simulator on invented input."""
import csv
import json
from pathlib import Path

import pytest
from shapely.geometry import box

from src.cli.main import main
from src.common.config import load_config
from src.data import p0
from src.data.sources import fingerprint
from src.data.tlc import COLUMNS
from src.network.prepared import PreparedNetwork
from src.m5.grid import RoadGrid

ROOT = Path(__file__).resolve().parents[2]


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf8")


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = Path("data/raw")
    raw.mkdir(parents=True)
    cfg = load_config(ROOT / "configs/p0_scc100.json")
    nodes = {i: (-74 + .001 * i, 40.75) for i in range(1, 12)}
    ways = [(1, list(range(1, 11)), {"highway": "primary"}),
            (2, [10, 11], {"highway": "primary", "oneway": "yes"}),
            (3, [1, 2], {"highway": "primary", "oneway": "yes"})]
    stations = [{"vertiport_id": str(i), "longitude": nodes[i][0], "latitude": nodes[i][1],
                 "transfer_time_seconds": 0} for i in range(1, 11)]
    stations[-1]["longitude"] = nodes[11][0] + .001
    routes = [{"route_id": f"{i}-{j}", "origin_id": str(i), "destination_id": str(j), "headway_seconds": 180}
              for i in range(1, 11) for offset in range(1, 7) for j in [1 + (i - 1 + offset) % 10]]
    sources = {k: Path(name) for k, name in cfg["inputs"].items()}
    dump(sources["air_routes"], routes)
    dump(sources["vertiports"], stations)
    sources["osm"].write_bytes(b"fixture substituting only the PBF reader and polygon lookup")
    with sources["tlc"].open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for hour in ("2013-05-04 22", "2013-05-05 00", "2013-05-07 00"):
            for i in range(22):
                lon, lat = nodes[1]
                destination = nodes[10] if i != 21 else nodes[2]  # raw OD < 500 m
                if i == 19:  # Previously unreachable leaf -> core, now within 100 m of node 10.
                    (lon, lat), destination = nodes[11], nodes[1]
                writer.writerow([hour + ":00:00", hour + ":04:00", lon, lat + (.005 if i == 20 else 0),
                                 *destination, 1, "1"])
    entries = {}
    for k, path in sources.items():
        archive = raw / path.name
        archive.write_bytes(path.read_bytes())
        entries[k] = dict(original_path=str(path), archive_path=archive.as_posix(), **fingerprint(path))
    dump(Path("data/source_manifest.json"), {"schema_version": 1, "sources": entries})
    monkeypatch.setattr(p0, "_boundary", lambda *args: box(-75, 40, -73, 41))
    monkeypatch.setattr(p0, "read_snapshot", lambda *args: (nodes, ways, {}))
    config = ROOT / "configs/p0_scc100.json"
    net = p0.prepare_network(config)
    requests = p0.prepare_data(config)
    return net, requests


def test_production_pipeline_and_atomic_service(instance, tmp_path, capsys):
    net, requests = instance
    graph = p0.read_json(net / "roads.json")
    assert len(graph["nodes"]) == 10 and len(graph["directed_edges"]) == 19
    manifest = p0.read_json(requests / "manifest.json")
    assert [manifest["official_samples"][k]["rows"] for k in ("WARMUP", "EVALUATION", "TAIL")] == [1, 1, 1]
    audit = p0.read_json(requests / "tlc_audit.json")
    assert [(r["removed"], r["remaining"]) for r in audit["cleaning_ledger"]][-3:] == [(3, 63), (3, 60), (0, 60)]
    cfg = load_config(ROOT / "configs/debug_10min_scc100.yaml")
    cfg["simulation"].update(warmup_start="2013-05-05T00:00:00", evaluation_end_exclusive="2013-05-05T00:00:10")
    dump(Path("tiny.json"), cfg)
    capsys.readouterr()
    assert main(["simulate", "--config", "tiny.json", "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dataset_variant"] == "scc100" and preview["road_nodes"] == 10
    network = PreparedNetwork.from_config(cfg)
    assert set(network.road.node_ids) == set(range(1, 11))
    for u in network.road.node_ids:
        for v in network.road.node_ids:
            node = u
            for edge in network.road.shortest_path_edges(u, v):
                assert edge.source == node and 1 <= edge.target <= 10
                node = edge.target
            assert node == v
    grid = RoadGrid.build(network.road)
    assert {n for entry in grid.entries.values() for n in entry} == set(network.road.node_ids)
    assert set(network.air.station_nodes.values()) == set(network.road.node_ids)
    changed = p0.read_json(net / "air_audit.json")["stations"][-1]
    assert (changed["full_road_node"], changed["road_node"]) == (11, 10)
    assert changed["snap_m"] > 100 and network.air.station_nodes["10"] == 10
    assert main(["simulate", "--config", "tiny.json", "--output-dir", "runs"]) == 0
    run, = Path("runs").iterdir()
    finished = p0.read_json(run / "manifest.json")
    assert finished["status"] == "COMPLETE" and finished["dataset_variant"] == "scc100"
    assert len(finished["initial_fleet_nodes"]) == 50
    assert set(finished["initial_fleet_nodes"].values()) <= set(network.road.node_ids)
    with (run / "requests.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert any(r["cohort"] == "EVALUATION" and r["state"] == "COMPLETED" for r in rows)
    with pytest.raises(FileExistsError):
        p0.prepare_network(ROOT / "configs/p0_scc100.json")
    with pytest.raises(FileExistsError):
        p0.prepare_data(ROOT / "configs/p0_scc100.json")


def test_audit_replays_full_population_and_queries_both_networks(instance):
    from src.data.scc100_audit import audit_population, check_induced_graph, _air
    from src.data.experiment import official_inputs
    net, requests = instance
    old_net = p0.prepare_network(ROOT / "configs/p0.json")
    old_requests = p0.prepare_data(ROOT / "configs/p0.json")
    full, selected = [p0.read_json(p / "roads.json") for p in (old_net, net)]
    graph = check_induced_graph(full, selected)
    assert (graph["full_nodes"], graph["full_edges"], graph["nodes"], graph["edges"]) == (11, 20, 10, 19)
    assert _air(p0.read_json(old_net / "air_audit.json"), p0.read_json(net / "air_audit.json"),
                selected["nodes"])["10"]["old_node"] == 11
    paths, _, _ = official_inputs(load_config(ROOT / "configs/debug_10min_scc100.yaml"))
    source = p0.read_json(Path("data/source_manifest.json"))["sources"]["tlc"]
    totals = audit_population(Path(source["archive_path"]), old_requests / "road_mapping_comparison.csv.gz",
                              requests, paths, selected["nodes"], p0.read_json(old_requests / "tlc_audit.json"))
    assert (totals["pre_road_candidates"], totals["snap_over_100m"], totals["eligible"],
            totals["sample_total"], totals["nearest_witness_endpoints"]) == (63, 3, 60, 3, 2)
    assert p0.read_json(old_requests / "tlc_audit.json")["removed_by_primary_reason"]["unreachable_road_od"] == 3
    # A synthetic historical approval, using the same full-network schema as P1.
    old_config = load_config(ROOT / "configs/debug_10min.yaml")
    old_config["inputs"]["prepared_request_manifest_path"] = "approved_tiny_full.json"
    dump(Path("approved_tiny_full.json"), {
        "status": "OFFICIAL_P0_INPUTS", "network_manifest": fingerprint(old_net / "manifest.json"),
        "official_road_network": dict(path=(old_net / "roads.json").as_posix(), directed=True,
                                      policy="full_directed_reachable_od", **fingerprint(old_net / "roads.json"))})
    a = PreparedNetwork.from_config(old_config)
    b = PreparedNetwork.from_config(load_config(ROOT / "configs/debug_10min_scc100.yaml"))
    assert 11 in a.road.node_ids and 11 not in b.road.node_ids and a.road.version != b.road.version
    assert (a.air.station_nodes["10"], b.air.station_nodes["10"]) == (11, 10)


def test_wrong_output_alias_fails_before_touching_baseline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(ROOT / "configs/p0_scc100.json")
    cfg["processed_dir"] = "data/processed"
    dump(Path("wrong.json"), cfg)
    with pytest.raises(ValueError, match="SCC100 processing configuration"):
        p0.prepare_network("wrong.json")
    assert not Path("data").exists()


def test_manual_gate_cli_reports_reference_differences_on_artificial_data(instance, monkeypatch, capsys):
    from src.data import scc100_audit
    config = ROOT / "configs/debug_10min_scc100.yaml"
    _, _, identity = scc100_audit.official_inputs(load_config(config), verify=True)
    p0.prepare_network(ROOT / "configs/p0.json")
    legacy_dir = p0.prepare_data(ROOT / "configs/p0.json")
    legacy_path = legacy_dir / "manifest.json"
    legacy = p0.read_json(legacy_path)
    candidate = legacy_dir / "candidate_manifest.json"
    candidate.write_bytes(legacy_path.read_bytes())
    checksum = legacy_dir / "evaluation_sample.sha256"
    checksum.write_text(legacy["artifacts"]["evaluation_full_graph_candidate.csv"]["sha256"])
    legacy.update(candidate_manifest=dict(path=str(candidate), **fingerprint(candidate)),
                  evaluation_sample_checksum=dict(path=str(checksum), **fingerprint(checksum)), formal_config=[])
    dump(legacy_path, legacy)
    # Only the historical approval targets NYC-specific counts. Supply its four source identities
    # for this tiny fixture; the new reader, on-disk audits and real SCC100 dry-run all execute.
    reader = scc100_audit.official_inputs
    def inputs(cfg, *, verify):
        if cfg["inputs"].get("dataset_variant") == "scc100":
            return reader(cfg, verify=verify)
        return [], None, {k: identity[k] for k in ("raw_tlc_sha256", "raw_osm_sha256",
                                                   "air_routes_sha256", "vertiports_sha256")}
    monkeypatch.setattr(scc100_audit, "official_inputs", inputs)
    capsys.readouterr()
    assert main(["audit-scc100", "--config", str(config)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "DATA_GATE FAIL"
    assert report["reference_differences"]["eligible"] == {"reference": 1059965, "observed": 60}
    assert report["e10_dry_run"]["input_counts"] == {"WARMUP": 1, "EVALUATION": 1, "TAIL": 1}
    assert not Path("outputs").exists()


@pytest.mark.parametrize("mixed", ["road", "manifest", "variant", "sample", "stamp", "air", "source", "removed_node"])
def test_cross_version_and_same_size_substitution_fail(instance, mixed):
    from src.data.experiment import official_inputs
    net, requests = instance
    cfg = load_config(ROOT / "configs/debug_10min_scc100.yaml")
    manifest = requests / "manifest.json"
    if mixed == "road":
        cfg["inputs"]["prepared_road_graph_path"] = "data/processed/p0_network/roads.json"
    elif mixed == "manifest":
        cfg["inputs"]["prepared_request_manifest_path"] = str(net / "manifest.json")
    elif mixed == "variant":
        cfg["inputs"].pop("dataset_variant")
    elif mixed == "sample":
        value = p0.read_json(manifest)
        value["official_samples"]["EVALUATION"]["path"] = str(requests / "tail_scc100.csv")
        dump(manifest, value)
    elif mixed == "stamp":
        path = requests / "evaluation_scc100.csv"
        path.write_bytes(path.read_bytes().replace(b"2013-05-05", b"2013-05-06"))
    elif mixed == "air":
        path = net / "air_audit.json"
        path.write_bytes(path.read_bytes().replace(b'"road_node": 1', b'"road_node": 2'))
    elif mixed == "removed_node":
        path = requests / "evaluation_scc100.csv"
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        rows[0]["origin_node"] = "11"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        value = p0.read_json(manifest)
        value["official_samples"]["EVALUATION"].update(fingerprint(path))
        value["artifacts"][path.name] = fingerprint(path)
        dump(manifest, value)
    else:
        cfg["inputs"]["raw_tlc_path"] = "different.csv"
    with pytest.raises((ValueError, KeyError)):
        official_inputs(cfg)


@pytest.mark.parametrize("name", ["debug_10min", "debug_30min", "sensitivity_5h", "paper_main"])
def test_profiles_preserve_e10_parameters(name):
    old = load_config(ROOT / f"configs/{name}.yaml")
    new = load_config(ROOT / f"configs/{name}_scc100.yaml")
    for section in ("experiment", "simulation", "requests", "m1", "m3", "m4", "m5", "validation", "reporting"):
        assert new[section] == old[section]
    assert new["data"]["seed"] == 20130505 and new["data"]["sample_rate"] == .05

