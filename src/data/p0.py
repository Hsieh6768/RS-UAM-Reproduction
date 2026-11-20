from importlib import metadata
from pathlib import Path
import json
import sys

import networkx as nx
import numpy as np
import pandas as pd
import pyogrio
from shapely import covers, points
from shapely.geometry import mapping, shape

from src.common.config import load_config
from src.common.geo import METERS_PER_MILE, mph_to_mps
from src.data.sources import archive_file, fingerprint, write_json
from src.data.tlc import COLUMNS, COORDINATES, PICKUP, PRIMARY_REASONS, audit_tlc, stratified_sample
from src.network.air import audit_air
from src.network.pbf import inspect_header, read_snapshot
from src.network.road import RoadNodeIndex, build_graph, map_requests, summarize_graph


SOURCE_TYPES = {"tlc": "TLC CSV", "osm": "OSM PBF", "air_routes": "route JSON", "vertiports": "vertiport JSON"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def environment():
    versions = {}
    for name in (
        "numpy", "pandas", "scipy", "networkx", "osmnx", "pyogrio", "shapely", "pyproj",
        "gurobipy", "rtree", "PyYAML", "pytest", "setuptools", "wheel",
    ):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "conda_environment": Path(sys.prefix).name,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "packages": versions,
    }


def inputs(config):
    archive = Path(config["archive_dir"])
    return {
        key: {"original_path": value, "archive_path": (archive / Path(value).name).as_posix(),
              "type": SOURCE_TYPES[key], **fingerprint(Path(value))}
        for key, value in config["inputs"].items()
    }


def _air(paths, network):
    return audit_air(
        read_json(paths["vertiports"]), read_json(paths["air_routes"]),
        mph_to_mps(network["evtol_speed_mph"]), network["max_air_route_miles"] * METERS_PER_MILE,
    )


def validate_inputs(config):
    sources = inputs(config)
    paths = {key: Path(value["original_path"]) for key, value in sources.items()}
    columns = pd.read_csv(paths["tlc"], nrows=0).columns.tolist()
    if set(columns) != set(COLUMNS):
        raise ValueError(f"Missing or unexpected TLC columns: {columns}")
    return {
        "environment": environment(), "sources": sources, "tlc_columns": columns,
        "osm": inspect_header(paths["osm"]), "air": _air(paths, config["network"]),
    }


def archive_inputs(config):
    evidence = validate_inputs(config)
    sources = evidence["sources"]
    manifest = Path(config["source_manifest"])
    manifest_value = {"schema_version": 1, "sources": sources}
    if manifest.exists() and read_json(manifest) != manifest_value:
        raise FileExistsError(f"Source manifest already records different inputs: {manifest}")
    for source in sources.values():
        archive = Path(source["archive_path"])
        expected = {key: source[key] for key in ("size_bytes", "sha256")}
        if archive.exists() and fingerprint(archive) != expected:
            raise FileExistsError(f"Archive differs from source: {archive}")
    for source in sources.values():
        archive_file(Path(source["original_path"]), Path(source["archive_path"]),
                     {key: source[key] for key in ("size_bytes", "sha256")})
    if not manifest.exists():
        write_json(manifest, manifest_value)
    return manifest_value


def _archive_paths(config):
    sources = read_json(Path(config["source_manifest"]))["sources"]
    result = {}
    for key, original in config["inputs"].items():
        entry = sources[key]
        archived = Path(entry["archive_path"])
        expected = {key: entry[key] for key in ("size_bytes", "sha256")}
        if original != entry["original_path"] or fingerprint(archived) != expected:
            raise ValueError(f"Archived {key} does not match configuration and source manifest")
        result[key] = archived
    return result


def _provenance(config_path):
    return {
        "config": {"path": Path(config_path).as_posix(), **fingerprint(Path(config_path))},
        "environment": environment(),
    }


def _boundary(path, relation_id):
    sql = (
        "SELECT * FROM multipolygons WHERE "
        f"osm_id = '{int(relation_id)}' AND boundary = 'administrative' AND name = 'Manhattan'"
    )
    matches = pyogrio.read_dataframe(path, sql=sql)
    if len(matches) != 1 or matches.crs.to_epsg() != 4326:
        raise ValueError(f"Could not uniquely resolve Manhattan boundary relation {relation_id} in WGS84")
    boundary = matches.geometry.iloc[0]
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError(f"Invalid Manhattan boundary geometry for relation {relation_id}")
    return boundary


def _write_artifact_manifest(directory, details):
    artifacts = {p.name: fingerprint(p) for p in sorted(directory.iterdir()) if p.is_file()}
    write_json(directory / "manifest.json", {**details, "artifacts": artifacts})


def prepare_network(config_path):
    config = load_config(config_path)
    paths = _archive_paths(config)
    settings = config["network"]
    if settings["road_profile"] != "p0_osmnx_drive_unsimplified" or (
        settings["road_component_policy"] != "compare_full_and_largest_strong"
    ):
        raise ValueError("Unsupported P0 road profile or component policy")
    output = Path(config["processed_dir"]) / "p0_network"
    if output.exists():
        raise FileExistsError(f"Network artifact directory already exists: {output}")
    relation = settings["manhattan_boundary_relation"]
    boundary = _boundary(paths["osm"], relation)
    print(f"P0: Manhattan relation {relation}; reading original PBF node IDs", flush=True)
    nodes, ways, header = read_snapshot(paths["osm"], boundary.bounds)
    xy = np.array(list(nodes.values()))
    inside = covers(boundary, points(xy))
    nodes = {node: position for (node, position), keep in zip(nodes.items(), inside) if keep}
    car_speed = mph_to_mps(settings["car_speed_mph"])
    graph = build_graph(nodes, ways, car_speed)
    stats, _, largest = summarize_graph(graph)
    air = _air(paths, settings)
    station_xy = np.array([(s["longitude"], s["latitude"]) for s in air["stations"]])
    inside = covers(boundary, points(station_xy))
    full_node, full_m, _ = RoadNodeIndex(graph, graph).nearest(station_xy)
    crop_node, crop_m, _ = RoadNodeIndex(graph, largest).nearest(station_xy)
    for i, station in enumerate(air["stations"]):
        station.update(
            inside_boundary=bool(inside[i]),
            full_road_node=int(full_node[i]), full_snap_m=float(full_m[i]),
            largest_road_node=int(crop_node[i]), largest_snap_m=float(crop_m[i]),
        )
    output.mkdir(parents=True)
    write_json(output / "boundary.geojson", {
        "type": "Feature", "properties": {"osm_relation_id": relation}, "geometry": mapping(boundary),
    })
    write_json(output / "roads.json", {
        "nodes": [[node, *data["position"]] for node, data in sorted(graph.nodes(data=True))],
        "directed_edges": [
            [a, b, data["way_id"], data["length_m"], data["time_s"]]
            for a, b, data in graph.edges(data=True)
        ],
    })
    write_json(output / "road_audit.json", {
        "pbf": header, "candidate_boundary_relation": relation, "boundary_bbox_wgs84": list(boundary.bounds),
        "pbf_nodes_in_manhattan": len(nodes), "car_speed_mps": car_speed,
        "evtol_speed_mps": mph_to_mps(settings["evtol_speed_mph"]),
        "parent_source_has_explicit_directed_graph": False, "component_policy": settings["road_component_policy"],
        **stats,
    })
    write_json(output / "air_audit.json", air)
    _write_artifact_manifest(output, {
        **_provenance(config_path), "source_manifest": fingerprint(Path(config["source_manifest"])),
        "status": "P0_NETWORK_AUDIT",
    })
    print(f"P0: {len(graph)} nodes, {graph.number_of_edges()} directed edges, {len(largest)} in largest SCC")
    return output


def _read_graph(path):
    network = read_json(path)
    graph = nx.MultiDiGraph()
    graph.add_nodes_from((node, {"position": (x, y)}) for node, x, y in network["nodes"])
    graph.add_edges_from(
        (a, b, {"way_id": way, "length_m": length, "time_s": time})
        for a, b, way, length, time in network["directed_edges"]
    )
    return graph


def _accounting(tlc, eligible, mapped, sampled, quotas):
    unreachable = ~mapped["road_reachable"]
    tlc["removed_by_primary_reason"]["unreachable_road_od"] = int(unreachable.sum())
    remaining = tlc["source_rows"]
    tlc["cleaning_ledger"] = []
    for reason in PRIMARY_REASONS:
        removed = tlc["removed_by_primary_reason"][reason]
        remaining -= removed
        tlc["cleaning_ledger"].append({"reason": reason, "removed": removed, "remaining": remaining})
    if remaining != len(eligible):
        raise RuntimeError("Filtering ledger does not close")
    tlc["road_eligible_full_graph"] = remaining
    tlc["sample_count_full_graph"] = len(sampled)
    tlc["actual_sample_rate_full_graph"] = len(sampled) / remaining if remaining else None
    tlc["sample_by_cohort"] = {}
    tlc["sample_by_date"] = {}
    for field, target in (("cohort", "sample_by_cohort"), ("date", "sample_by_date")):
        rolled = quotas.assign(date=quotas["hour"].str.slice(0, 10)).groupby(field)[["eligible", "sampled"]].sum()
        for name, row in rolled.iterrows():
            tlc[target][name] = {
                "eligible": int(row["eligible"]), "sampled": int(row["sampled"]),
                "actual_rate": float(row["sampled"] / row["eligible"]),
            }


def prepare_data(config_path):
    config = load_config(config_path)
    paths = _archive_paths(config)
    processed = Path(config["processed_dir"])
    network = processed / "p0_network"
    network_manifest = read_json(network / "manifest.json")
    if (
        network_manifest["source_manifest"] != fingerprint(Path(config["source_manifest"]))
        or network_manifest["config"]["sha256"] != fingerprint(Path(config_path))["sha256"]
    ):
        raise ValueError("Network artifacts use a different configuration or input manifest")
    for name, expected in network_manifest["artifacts"].items():
        if fingerprint(network / name) != expected:
            raise ValueError(f"Network artifact checksum mismatch: {name}")
    output = processed / "p0_requests"
    if output.exists():
        raise FileExistsError(f"Request artifact directory already exists: {output}")
    graph = _read_graph(network / "roads.json")
    boundary = shape(read_json(network / "boundary.geojson")["geometry"])
    _, components, largest = summarize_graph(graph)
    req = config["requests"]
    windows = tuple(req[key] for key in ("warmup_start", "evaluation_start", "evaluation_end_exclusive"))
    print("P0: auditing all TLC rows and testing both road mapping policies", flush=True)
    candidates, tlc = audit_tlc(paths["tlc"], boundary, req["max_party"], req["min_direct_od_m"], windows)
    positions = candidates[COORDINATES].astype(float).to_numpy()
    mapped, impact = map_requests(graph, components, largest, positions[:, :2], positions[:, 2:])
    tlc["road_mapping_policy_comparison"] = impact
    tlc["road_mapping_comparison_by_cohort"] = {}
    for cohort, group in candidates.groupby("cohort"):
        cohort_map = mapped.loc[group.index]
        changed = (cohort_map["origin_node"] != cohort_map["largest_origin_node"]) | (
            cohort_map["destination_node"] != cohort_map["largest_destination_node"]
        )
        tlc["road_mapping_comparison_by_cohort"][cohort] = {
            "pre_road_candidates": len(group),
            "remapped_by_largest_scc": int(changed.sum()),
            "full_graph_unreachable": int((~cohort_map["road_reachable"]).sum()),
        }
    eligible = candidates.join(mapped).loc[mapped["road_reachable"]].sort_values([PICKUP, "request_id"])
    sampled, quotas = stratified_sample(eligible, req["sample_rate"], req["sample_seed"])
    _accounting(tlc, eligible, mapped, sampled, quotas)
    output.mkdir(parents=True)
    # Provide evidence for both profiles. A finalized sample depends on the road policy decision.
    candidates[["request_id", "cohort"]].join(mapped).to_csv(output / "road_mapping_comparison.csv.gz",
        index=False, lineterminator="\n", compression={"method": "gzip", "mtime": 0})
    columns = list(candidates.columns) + [
        "origin_node", "destination_node", "origin_snap_m", "destination_snap_m", "mapped_direct_od_m",
    ]
    eligible[columns].to_csv(output / "eligible_full_graph_candidate.csv.gz",
        index=False, lineterminator="\n", compression={"method": "gzip", "mtime": 0})
    for cohort in ("WARMUP", "EVALUATION", "TAIL"):
        subset = sampled.loc[sampled["cohort"].eq(cohort), columns]
        subset.to_csv(output / f"{cohort.lower()}_full_graph_candidate.csv", index=False, lineterminator="\n")
    quotas.to_csv(output / "sampling_by_hour.csv", index=False, lineterminator="\n")
    write_json(output / "tlc_audit.json", tlc)
    _write_artifact_manifest(output, {
        **_provenance(config_path),
        "status": "CANDIDATE_PENDING_FINAL_ROAD_POLICY",
        "network_manifest": fingerprint(network / "manifest.json"),
        "source_manifest": fingerprint(Path(config["source_manifest"])),
        "sample_seed": req["sample_seed"], "sample_rate": req["sample_rate"],
    })
    print(f"P0: {tlc['source_rows']} source rows, {len(eligible)} eligible, {len(sampled)} candidate sample rows")
    return output
