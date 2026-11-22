"""Read-only, manual data gate against the archived P0 inputs and regenerated SCC100."""
from collections import Counter, defaultdict
import csv
from datetime import datetime
from fractions import Fraction
import gzip
from itertools import zip_longest
from math import isclose
from pathlib import Path
from random import Random

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from src.common.config import load_config
from src.data.experiment import official_inputs
from src.data.instance import check_file, read_json
from src.data.scc100 import COHORTS, CONFIGS
from src.data.tlc import COLUMNS, PICKUP, PRIMARY_REASONS
from src.network.prepared import PreparedNetwork


REFERENCE = {"full_nodes": 18713, "full_edges": 26461, "nodes": 16808, "edges": 24491,
             "pre_road_candidates": 1084915, "snap_over_100m": 24950, "eligible": 1059965,
             "WARMUP": 2440, "EVALUATION": 32882, "TAIL": 17677, "sample_total": 52999}


def require(condition, detail):
    if not condition:
        raise ValueError(f"SCC100 data audit: {detail}")


def check_induced_graph(full, selected):
    ids = sorted(n for n, _, _ in full["nodes"])
    index = {n: i for i, n in enumerate(ids)}
    require(len(index) == len(ids) and ids, "duplicate or empty P0 node table")
    edges = full["directed_edges"]
    rows = [index[e[0]] for e in edges]
    cols = [index[e[1]] for e in edges]
    matrix = coo_matrix((np.ones(len(edges)), (rows, cols)), shape=(len(ids), len(ids)))
    count, labels = connected_components(matrix, directed=True, connection="strong")
    groups = defaultdict(list)
    for n, label in zip(ids, labels):
        groups[label].append(n)
    keep = set(min(groups.values(), key=lambda g: (-len(g), g[0])))
    require(sorted(row for row in full["nodes"] if row[0] in keep) == sorted(selected["nodes"]),
            "largest SCC node IDs or coordinates differ from independent sparse-graph result")
    expected = Counter(tuple(e) for e in edges if e[0] in keep and e[1] in keep)
    require(expected == Counter(map(tuple, selected["directed_edges"])),
            "induced directed edges, multiplicities, way IDs, lengths or times differ")
    return dict(full_nodes=len(ids), full_edges=len(edges), full_sccs=int(count), nodes=len(keep),
                edges=len(selected["directed_edges"]), strong_components=1)


def nearest_oracle(nodes, point):
    """Independent exhaustive haversine search, including coincident OSM nodes."""
    table = np.asarray(sorted(nodes), dtype=float)
    lon, lat = np.radians(table[:, 1:]).T
    x, y = np.radians(point)
    a = np.sin((lat - y) / 2)**2 + np.cos(y) * np.cos(lat) * np.sin((lon - x) / 2)**2
    i = np.argmin(a)
    return int(table[i, 0]), float(2 * 6371009.0 * np.arcsin(np.sqrt(np.clip(a[i], 0, 1))))


def _verify_artifacts(manifest_path):
    manifest = read_json(manifest_path)
    for name, ref in manifest["artifacts"].items():
        require(Path(name).name == name, f"invalid local artifact name {name}")
        check_file(manifest_path.with_name(name), ref)
    return manifest


def _air(old, new, selected_nodes):
    require(old["routes"] == new["routes"] and len(new["routes"]) == 60, "directed air routes/times")
    previous = {s["vertiport_id"]: s for s in old["stations"]}
    require(len(previous) == len(new["stations"]) == 10, "vertiport counts")
    mapping = {}
    for station in new["stations"]:
        name = station["vertiport_id"]
        original = previous[name]
        require(all(station[k] == v for k, v in original.items()), f"historical station data at {name}")
        node, metres = nearest_oracle(selected_nodes, (station["longitude"], station["latitude"]))
        require(node == station["road_node"] == original["largest_road_node"] and
                isclose(metres, station["snap_m"], abs_tol=1e-6), f"station snap at {name}")
        mapping[name] = dict(old_node=original["full_road_node"], old_snap_m=original["full_snap_m"],
                             node=node, snap_m=metres)
    return mapping


def _sample_rows(paths):
    result = {}
    for cohort, path in zip(COHORTS, paths):
        previous = None
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                rid = int(row["request_id"])
                key = row[PICKUP], rid
                require(rid not in result and row["cohort"] == cohort and
                        (previous is None or previous <= key), f"sample ID, cohort or order at {rid}")
                result[rid] = row
                previous = key
    return result


def _check_quotas(groups, quota_path, sample_ids):
    # Independent integer allocation; ties between equal remainders go to the earlier hour.
    expected = {key: len(ids) // 20 for key, ids in groups.items()}
    for cohort in COHORTS:
        keys = [k for k in groups if k[0] == cohort]
        seats = round(Fraction(sum(len(groups[k]) for k in keys), 20)) - sum(expected[k] for k in keys)
        for key in sorted(keys, key=lambda k: (-(len(groups[k]) % 20), k[1]))[:seats]:
            expected[key] += 1
    with quota_path.open(encoding="utf-8-sig", newline="") as stream:
        recorded = {}
        for row in csv.DictReader(stream):
            k = (row["cohort"], row["hour"])
            require(k not in recorded, f"duplicate hour {k}")
            recorded[k] = int(row["eligible"]), int(row["sampled"])
    require(recorded == {k: (len(groups[k]), q) for k, q in expected.items()}, "hourly quotas")
    rng = Random(20130505)
    chosen = set()
    for key in sorted(groups):
        chosen.update(rng.sample(groups[key], expected[key]))
    require(chosen == sample_ids, "seeded sample IDs differ from replay")


def audit_population(raw_csv, historical_mapping, request_dir, sample_paths, nodes, old_audit):
    """Compare every base-eligible ID with the historical *pre-sampling* mapping and raw record."""
    audit = read_json(request_dir / "tlc_audit.json")
    samples = _sample_rows(sample_paths)
    node_ids = {n for n, _, _ in nodes}
    groups, counts = defaultdict(list), Counter()
    eligible_samples = set()
    warm_end, eval_end = datetime(2013, 5, 5), datetime(2013, 5, 7)
    with raw_csv.open(encoding="utf-8-sig", newline="") as raw, \
            gzip.open(historical_mapping, "rt", newline="") as historical, \
            gzip.open(request_dir / "mapped_population.csv.gz", "rt", newline="") as current:
        source = enumerate(csv.DictReader(raw), start=1)
        last = 0
        pairs = zip_longest(csv.DictReader(historical), csv.DictReader(current))
        for i, (old, row) in enumerate(pairs):
            require(old and row and old["request_id"] == row["request_id"], f"base population differs at row {i}")
            rid = int(row["request_id"])
            require(rid > last, f"duplicate or unordered population at {rid}")
            for line, original in source:
                if line == rid:
                    break
            else:
                raise ValueError(f"Unknown raw request ID {rid}")
            last = rid
            require(all(row[k] == original[k] for k in COLUMNS), f"source fields at {rid}")
            time = datetime.fromisoformat(original[PICKUP])
            cohort = "WARMUP" if time < warm_end else ("EVALUATION" if time < eval_end else "TAIL")
            hour = time.strftime("%Y-%m-%dT%H")
            require(row["cohort"] == old["cohort"] == cohort and row["hour"] == hour and
                    datetime.fromisoformat(row["request_time_local"]) == time, f"release/cohort at {rid}")
            for endpoint, prefix in (("origin", "pickup"), ("destination", "dropoff")):
                key, distance = endpoint + "_node", endpoint + "_snap_m"
                node, metres = int(row[key]), float(row[distance])
                require(node in node_ids and row[key] == old["largest_" + key] and
                        0 <= metres and isclose(metres, float(old["largest_" + distance]), abs_tol=1e-7),
                        f"new mapping / historical full-population SCC projection at {rid}:{endpoint}")
                if i % 4096 == 0:
                    candidate, actual_m = nearest_oracle(nodes, (float(row[prefix + "_longitude"]),
                                                                  float(row[prefix + "_latitude"])))
                    require(candidate == node and isclose(actual_m, metres, abs_tol=1e-6),
                            f"independent nearest-node witness at {rid}:{endpoint}")
                    counts["nearest_witness_endpoints"] += 1
            o_over, d_over = (float(row[k]) > 100 for k in ("origin_snap_m", "destination_snap_m"))
            counts["origin_over_100m"] += o_over
            counts["destination_over_100m"] += d_over
            counts["both_over_100m"] += o_over and d_over
            counts["pre_road_candidates"] += 1
            counts["mapped_same_node"] += row["origin_node"] == row["destination_node"]
            keep = not (o_over or d_over)
            require(row["eligible"] == str(keep) and float(row["raw_direct_od_m"]) >= 500, f"eligibility at {rid}")
            if keep:
                require(row["origin_node"] != row["destination_node"], f"zero-time order {rid}")
                groups[cohort, hour].append(rid)
                counts["eligible"] += 1
            else:
                counts["snap_over_100m"] += 1
            if rid in samples:
                require(keep and {k: v for k, v in row.items() if k != "eligible"} == samples[rid],
                        f"sample payload differs from population at {rid}")
                eligible_samples.add(rid)
                counts[cohort] += 1
        source_rows = last + sum(1 for _ in source)
    require(eligible_samples == samples.keys(), "sample IDs missing from population")
    _check_quotas(groups, request_dir / "sampling_by_hour.csv", eligible_samples)
    reasons = dict(old_audit["removed_by_primary_reason"], snap_over_100m=counts["snap_over_100m"],
                   unreachable_road_od=0)
    remaining = source_rows
    expected_ledger = []
    for reason in (*PRIMARY_REASONS[:-1], "snap_over_100m", "unreachable_road_od"):
        remaining -= reasons[reason]
        expected_ledger.append(dict(reason=reason, removed=reasons[reason], remaining=remaining))
    require(source_rows == old_audit["source_rows"] == audit["source_rows"] and
            reasons == audit["removed_by_primary_reason"] and expected_ledger == audit["cleaning_ledger"] and
            remaining == counts["eligible"] == audit["eligible_count"], "raw and cleaned ledger reconciliation")
    require(all(counts[k] == audit[k] for k in ("pre_road_candidates", "mapped_same_node",
                "origin_over_100m", "destination_over_100m", "both_over_100m")), "snap audit totals")
    return dict(counts, sample_total=len(samples), source_rows=source_rows, hours=len(groups),
                cleaning_ledger=expected_ledger)


def audit_scc100(config_path):
    """Manual Gate B only. Never creates a run or dispatches a vehicle."""
    config = load_config(config_path)
    require(config == load_config(CONFIGS / "debug_10min_scc100.yaml"), "Gate B requires the SCC100 E10 profile")
    current_paths, _, meta = official_inputs(config, verify=True)
    historical = load_config(CONFIGS / "debug_10min.yaml")
    _, _, old_meta = official_inputs(historical, verify=True)
    for key in ("raw_tlc_sha256", "raw_osm_sha256", "air_routes_sha256", "vertiports_sha256"):
        require(meta[key] == old_meta[key], f"old/new source identity {key}")
    sources = read_json(Path("data/source_manifest.json"))["sources"]
    for value in sources.values():
        for key in ("original_path", "archive_path"):
            check_file(Path(value[key]), value)
    new, old = [Path(c["inputs"]["prepared_road_graph_path"]).parent for c in (config, historical)]
    requests, old_requests = [Path(c["inputs"]["prepared_request_manifest_path"]).parent
                              for c in (config, historical)]
    for directory in (new, old, requests, old_requests):
        _verify_artifacts(directory / "manifest.json")
    legacy = read_json(old_requests / "manifest.json")
    for ref in [legacy["config"], legacy["candidate_manifest"], legacy["evaluation_sample_checksum"],
                *legacy["formal_config"]]:
        check_file(Path(ref["path"]), ref)
    full, selected = read_json(old / "roads.json"), read_json(new / "roads.json")
    network = check_induced_graph(full, selected)
    built = read_json(new / "road_audit.json")
    for key in ("nodes", "edges", "strong_components"):
        full_key = "full_sccs" if key == "strong_components" else "full_" + key
        require(built[key] == network[key] and built["full_graph"][key] == network[full_key],
                f"regenerated PBF full/cropped {key} differ from independent counts")
    require(read_json(new / "boundary.geojson") == read_json(old / "boundary.geojson"), "Manhattan boundary")
    stations = _air(read_json(old / "air_audit.json"), read_json(new / "air_audit.json"), selected["nodes"])
    demand = audit_population(Path(sources["tlc"]["archive_path"]), old_requests / "road_mapping_comparison.csv.gz",
                              requests, current_paths, selected["nodes"], read_json(old_requests / "tlc_audit.json"))
    require(all(meta["input_counts"][c] == demand.get(c, 0) for c in COHORTS), "runtime/sample cohort counts")
    loaded = PreparedNetwork.from_config(config)
    require(len(loaded.road.node_ids) == network["nodes"] and len(loaded.air.routes) == 60, "runtime loader")
    combined = {**network, **demand}
    differences = {k: {"reference": v, "observed": combined.get(k, 0)} for k, v in REFERENCE.items()
                   if combined.get(k, 0) != v}
    return {"status": "DATA_GATE FAIL" if differences else "DATA_GATE PASS",
            "reference_differences": differences, "network": network, "requests": demand,
            "stations": stations, "input_identity": meta, "historical_input_identity": old_meta}

