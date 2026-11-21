"""Read the locked P0 samples on their common New York local timeline (D011–D013)."""
import csv
from datetime import datetime, timedelta
from functools import partial
import json
from pathlib import Path
from random import Random

from src.data.sources import fingerprint
from src.m2.passengers import Request

COHORTS = ("WARMUP", "EVALUATION", "TAIL")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _check(path, ref, verify):
    if path.stat().st_size != ref["size_bytes"]:
        raise ValueError(f"Incorrect P0 input size: {path}")
    if verify and fingerprint(path)["sha256"] != ref["sha256"]:
        raise ValueError(f"Incorrect P0 input SHA256: {path}")


def official_inputs(config, *, verify=False):
    """Dry inspection reads only small manifests/audit, headers and file sizes."""
    inputs = config["inputs"]
    manifest_path = Path(inputs["prepared_request_manifest_path"])
    manifest = _read_json(manifest_path)
    road = manifest["official_road_network"]
    if (manifest["status"] != "OFFICIAL_P0_INPUTS" or not road["directed"]
            or road["policy"] != "full_directed_reachable_od" or (road["nodes"], road["edges"]) != (18713, 26461)
            or Path(inputs["prepared_road_graph_path"]).resolve() != Path(road["path"]).resolve()):
        raise ValueError("Expected approved P0 network and requests")
    _check(Path(road["path"]), road, verify)
    network_manifest = Path(road["path"]).with_name("manifest.json")
    _check(network_manifest, manifest["network_manifest"], verify)
    artifacts = _read_json(network_manifest)["artifacts"]
    for filename in ("road_audit.json", "air_audit.json"):
        _check(network_manifest.with_name(filename), artifacts[filename], verify)
    samples = manifest["official_samples"]
    paths = [Path(samples[key]["path"]) for key in COHORTS]
    required = {"request_id", "request_time_local", "origin_node", "destination_node", "passenger_count",
                "pickup_longitude", "pickup_latitude", "dropoff_longitude", "dropoff_latitude"}
    for key, path in zip(COHORTS, paths):
        _check(path, samples[key], verify)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            if not required.issubset(next(csv.reader(stream))):
                raise ValueError(f"Missing P0 sample columns: {path}")
    counts = {k: samples[k]["rows"] for k in COHORTS}
    if tuple(counts.values()) != (2490, 33565, 18055):
        raise ValueError("Official sample counts differ from P0")
    audit = manifest_path.with_name("tlc_audit.json")
    _check(audit, manifest["artifacts"][audit.name], verify)
    end = datetime.fromisoformat(_read_json(audit)["pickup_time_range"][1]) + timedelta(seconds=1)
    source_path = Path("data/source_manifest.json")
    _check(source_path, manifest["source_manifest"], verify)
    sources = _read_json(source_path)["sources"]
    return paths, end, {
        "input_counts": counts, "seed_data": manifest["sample_seed"],
        "processed_sample_sha256": {k: samples[k]["sha256"] for k in COHORTS},
        "road_network_sha256": road["sha256"], "air_routes_sha256": sources["air_routes"]["sha256"],
        "raw_tlc_sha256": sources["tlc"]["sha256"], "raw_osm_sha256": sources["osm"]["sha256"],
        "source_hash_provenance": "P0 archived-source manifest",
        "input_manifest_sha256": fingerprint(manifest_path)["sha256"],
        "air_audit_sha256": artifacts["air_audit.json"]["sha256"],
        "vertiports_sha256": sources["vertiports"]["sha256"], "sample_paths": list(map(str, paths)),
        "coverage_end_exclusive": end.isoformat(),
    }


def inspect_sample_counts(paths, counts, start, end):
    """Independent evaluation denominator, including zero-demand intervals."""
    expected = 0
    previous = None
    ids = set()
    for name, path in zip(COHORTS, paths):
        count = 0
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                at = datetime.fromisoformat(row["request_time_local"])
                rid = row["request_id"]
                if rid in ids or previous is not None and at < previous or at.microsecond:
                    raise ValueError(f"Duplicate, subsecond or out-of-order P0 release: {rid}")
                ids.add(rid)
                previous = at
                expected += start <= at < end
                count += 1
        if count != counts[name]:
            raise ValueError(f"Incorrect {name} sample count: {count}")
    return expected


def _released_request(row, at, router, pickup, delay):
    origin, dest = int(row["origin_node"]), int(row["destination_node"])
    return Request(row["request_id"], at, origin, dest, int(row["passenger_count"]),
                   router.shortest_time_s(origin, dest), pickup, delay,
                   original_origin=(float(row["pickup_longitude"]), float(row["pickup_latitude"])),
                   original_destination=(float(row["dropoff_longitude"]), float(row["dropoff_latitude"])))


def sample_envelopes(paths, origin, router, pickup, delay):
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                time = datetime.fromisoformat(row["request_time_local"])
                if time >= origin:
                    at = int((time - origin).total_seconds())
                    yield at, row["request_id"], partial(_released_request, row, at, router, pickup, delay)


def place_fleet(sim, size, seed, capacity):
    """Prefix of one seeded uniform sequence, with replacement; IDs survive warmup."""
    rng = Random(seed)
    for i in range(size):
        sim.add_vehicle(f"V{i + 1:05d}", rng.choice(sim.router.node_ids), capacity)

