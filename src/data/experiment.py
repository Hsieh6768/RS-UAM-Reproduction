"""Read the locked P0 samples on their common New York local timeline (D011–D013)."""
import csv
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from random import Random

from src.data.sources import fingerprint
from src.data.instance import check_file, network_references, read_json
from src.m2.passengers import Request

COHORTS = ("WARMUP", "EVALUATION", "TAIL")


def _sample_membership(path, cohort, nodes, expected_count, known_ids):
    """Fail before dispatch even when a stale node was copied into a newly hashed CSV."""
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            count += 1
            origin, dest = int(row["origin_node"]), int(row["destination_node"])
            rid = row["request_id"]
            if (origin not in nodes or dest not in nodes or origin == dest or row["cohort"] != cohort
                    or rid in known_ids or not 0 <= float(row["origin_snap_m"]) <= 100
                    or not 0 <= float(row["destination_snap_m"]) <= 100 or not float(row["raw_direct_od_m"]) >= 500
                    or datetime.fromisoformat(row["request_time_local"]) != datetime.fromisoformat(row["tpep_pickup_datetime"])):
                raise ValueError(f"SCC100 sample ID, release, OD or snap mismatch in {path} at {rid}")
            known_ids.add(rid)
    if count != expected_count:
        raise ValueError(f"SCC100 sample count differs from actual rows in {path}")


def official_inputs(config, *, verify=False):
    """Check selected artifacts. SCC100 also fingerprints simulation inputs during dry-run."""
    inputs = config["inputs"]
    manifest_path = Path(inputs["prepared_request_manifest_path"])
    selected = inputs.get("dataset_variant") == "scc100"
    verify = verify or selected
    manifest, net, roads = network_references(config, Path("."), verify)
    road = manifest["official_road_network"]
    if not selected and (road["nodes"], road["edges"]) != (18713, 26461):
        raise ValueError("Expected the approved full P0 network counts")
    artifacts = net["artifacts"]
    samples = manifest["official_samples"]
    paths = [Path(samples[key]["path"]) for key in COHORTS]
    required = {"request_id", "request_time_local", "origin_node", "destination_node", "passenger_count",
                "pickup_longitude", "pickup_latitude", "dropoff_longitude", "dropoff_latitude"}
    known_ids = set()
    if selected:
        graph = read_json(roads)
        nodes = {node for node, _, _ in graph["nodes"]}
        if (len(nodes), len(graph["directed_edges"])) != (road["nodes"], road["edges"]):
            raise ValueError("SCC100 serialized network counts differ from manifest")
        air = read_json(roads.with_name("air_audit.json"))
        if (len(air["stations"]) != 10 or len(air["routes"]) != 60 or
                any(s["road_node"] != s["largest_road_node"] or s["road_node"] not in nodes for s in air["stations"])):
            raise ValueError("SCC100 station mapping or directed air topology is inconsistent")
        required.update({"cohort", "origin_snap_m", "destination_snap_m", "raw_direct_od_m", "tpep_pickup_datetime"})
    for key, path in zip(COHORTS, paths):
        if selected:
            expected_path = manifest_path.parent / f"{key.lower()}_scc100.csv"
            if (path.resolve() != expected_path.absolute() or
                    any(samples[key][k] != manifest["artifacts"][path.name][k] for k in ("sha256", "size_bytes"))):
                raise ValueError(f"Sample does not belong to selected SCC100 instance: {path}")
        check_file(path, samples[key], verify)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            if not required.issubset(next(csv.reader(stream), [])):
                raise ValueError(f"Missing P0 sample columns: {path}")
        if selected:
            _sample_membership(path, key, nodes, samples[key]["rows"], known_ids)
    counts = {k: samples[k]["rows"] for k in COHORTS}
    if not selected and tuple(counts.values()) != (2490, 33565, 18055):
        raise ValueError("Official sample counts differ from P0")
    audit = manifest_path.with_name("tlc_audit.json")
    check_file(audit, manifest["artifacts"][audit.name], verify)
    ledger = read_json(audit)
    if selected:
        if (ledger["sample_by_cohort"] != {k: {"sampled": counts[k], "eligible": samples[k]["eligible_rows"]}
                                          for k in COHORTS}
                or sum(counts.values()) != manifest["sample_count"] or ledger["sample_count"] != manifest["sample_count"]
                or sum(v["eligible_rows"] for v in samples.values()) != manifest["eligible_count"]
                or manifest["eligible_count"] != ledger["eligible_count"]):
            raise ValueError("SCC100 sample counts disagree with the cleaning ledger")
    end = datetime.fromisoformat(ledger["pickup_time_range"][1]) + timedelta(seconds=1)
    source_path = Path("data/source_manifest.json")
    check_file(source_path, manifest["source_manifest"], verify)
    sources = read_json(source_path)["sources"]
    return paths, end, {
        "dataset_variant": "scc100" if selected else "full", "road_policy": road["policy"],
        "snap_max_distance_m": 100 if selected else None, "road_nodes": road["nodes"], "road_edges": road["edges"],
        "network_manifest_sha256": manifest["network_manifest"]["sha256"],
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


def sample_releases(paths, origin):
    """Yield release ticks and source rows without constructing requests or routing."""
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                time = datetime.fromisoformat(row["request_time_local"])
                if time >= origin:
                    at = int((time - origin).total_seconds())
                    yield at, row


def sample_envelopes(paths, origin, router, pickup, delay):
    for at, row in sample_releases(paths, origin):
        yield at, row["request_id"], partial(_released_request, row, at, router, pickup, delay)


def place_fleet(sim, size, seed, capacity):
    """Prefix of one seeded uniform sequence, with replacement; IDs survive warmup."""
    rng = Random(seed)
    for i in range(size):
        sim.add_vehicle(f"V{i + 1:05d}", rng.choice(sim.router.node_ids), capacity)

