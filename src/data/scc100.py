"""P6-NET approved service area and data instance, layered on the P0 filters."""
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
from tempfile import mkdtemp

import networkx as nx
import numpy as np
from shapely.geometry import shape

from src.common.config import load_config
from src.common.geo import distance_m
from src.data.sources import code_identity, fingerprint, write_json
from src.data.tlc import COORDINATES, PICKUP, PRIMARY_REASONS, audit_tlc, stratified_sample
from src.network.road import RoadNodeIndex, read_road_graph

DIRECTORY = Path("data/processed/p0_scc100")
COHORTS = ("WARMUP", "EVALUATION", "TAIL")
CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def is_scc100(config):
    if "dataset_variant" not in config:
        if config["network"]["road_component_policy"] != "compare_full_and_largest_strong":
            raise ValueError("Unrecognized legacy P0 component policy")
        return False
    expected = load_config(CONFIGS / "p0.json", {
        "schema_version": 2, "dataset_variant": "scc100", "processed_dir": DIRECTORY.as_posix(),
        "network": {"road_component_policy": "largest_scc"}, "requests": {"snap_max_distance_m": 100},
    })
    if config != expected or (config["requests"]["sample_rate"], config["requests"]["sample_seed"],
                              config["requests"]["min_direct_od_m"]) != (.05, 20130505, 500):
        raise ValueError("Expected the approved SCC100 processing configuration")
    if DIRECTORY.resolve() != Path.cwd() / DIRECTORY:
        raise ValueError("SCC100 output path redirects outside its own instance")
    return True


@contextmanager
def publish_directory(target):
    """Only the completed, checksummed directory becomes visible to readers."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        yield staging
        if target.exists():
            raise FileExistsError(target)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def variant_provenance(config):
    source = json.loads(Path(config["source_manifest"]).read_text(encoding="utf-8-sig"))
    return {
        "schema_version": 2, "dataset_variant": "scc100", "road_policy": "largest_scc",
        "snap_max_distance_m": 100, "sample_rate": .05, "sample_seed": 20130505,
        "p0_config": config, "base_config": fingerprint(CONFIGS / "p0.json"),
        "source_sha256": {k: v["sha256"] for k, v in source["sources"].items()},
        "code": code_identity(),
    }


def within_snap_limit(origin_m, destination_m, maximum_m):
    return (origin_m <= maximum_m) & (destination_m <= maximum_m)


def write_scc_requests(config, paths, network, output):
    graph = read_road_graph(network / "roads.json")
    if not nx.is_strongly_connected(graph):
        raise ValueError("Prepared graph does not consist of exactly one SCC")
    boundary = shape(json.loads((network / "boundary.geojson").read_text(encoding="utf8"))["geometry"])
    req = config["requests"]
    windows = tuple(req[k] for k in ("warmup_start", "evaluation_start", "evaluation_end_exclusive"))
    candidates, audit = audit_tlc(paths["tlc"], boundary, req["max_party"], req["min_direct_od_m"], windows)
    # A midnight-only CSV subset would otherwise format datetime64 as a bare date.
    candidates["request_time_local"] = candidates[PICKUP]
    xy = candidates[COORDINATES].to_numpy(dtype=float)
    index = RoadNodeIndex(graph, graph)
    o, om, origin_xy = index.nearest(xy[:, :2])
    d, dm, dest_xy = index.nearest(xy[:, 2:])
    keep = within_snap_limit(om, dm, req["snap_max_distance_m"])
    same = o == d
    # Triangle inequality: two 100 m snaps to one node imply raw OD <= 200 m.
    if np.any(same & keep):
        raise ValueError("Same-node OD survived both the raw 500 m and endpoint filters")
    candidates["origin_node"], candidates["destination_node"] = o, d
    candidates["origin_snap_m"], candidates["destination_snap_m"] = om, dm
    candidates["mapped_direct_od_m"] = distance_m(origin_xy, dest_xy)
    eligible = candidates.loc[keep]
    sample, quotas = stratified_sample(eligible, req["sample_rate"], req["sample_seed"])
    excluded = int((~keep).sum())
    audit.update(eligible_count=len(eligible), mapped_same_node=int(same.sum()),
                 eligible_same_node=int((same & keep).sum()), sample_count=len(sample),
                 origin_over_100m=int((om > 100).sum()), destination_over_100m=int((dm > 100).sum()),
                 both_over_100m=int(((om > 100) & (dm > 100)).sum()))
    reasons = audit["removed_by_primary_reason"]
    reasons["snap_over_100m"] = excluded
    # The single-SCC certificate covers every ordered pair of index members.
    reasons["unreachable_road_od"] = 0
    remaining = audit["source_rows"]
    ledger = []
    for reason in (*PRIMARY_REASONS[:-1], "snap_over_100m", "unreachable_road_od"):
        remaining -= reasons[reason]
        ledger.append(dict(reason=reason, removed=reasons[reason], remaining=remaining))
    if remaining != len(eligible):
        raise ValueError("SCC100 request ledger does not close")
    audit["cleaning_ledger"] = ledger
    final_dir = DIRECTORY / "requests"
    samples = {}
    for cohort in COHORTS:
        rows = sample.loc[sample["cohort"].eq(cohort)]
        path = output / f"{cohort.lower()}_scc100.csv"
        rows.to_csv(path, index=False, lineterminator="\n")
        samples[cohort] = dict(path=(final_dir / path.name).as_posix(), rows=len(rows),
                               eligible_rows=int(eligible["cohort"].eq(cohort).sum()), **fingerprint(path))
    audit["sample_by_cohort"] = {k: {"eligible": v["eligible_rows"], "sampled": v["rows"]}
                                 for k, v in samples.items()}
    # Retain original coordinates and snap distances even for rejected endpoints.
    candidates.assign(eligible=keep).to_csv(output / "mapped_population.csv.gz", index=False,
        lineterminator="\n", compression={"method": "gzip", "mtime": 0})
    quotas.to_csv(output / "sampling_by_hour.csv", index=False, lineterminator="\n")
    write_json(output / "tlc_audit.json", audit)
    roads = network / "roads.json"
    return {"official_samples": samples, "eligible_count": len(eligible), "sample_count": len(sample),
            "official_road_network": {"path": roads.as_posix(), **fingerprint(roads), "directed": True,
                                     "policy": "largest_scc", "nodes": len(graph), "edges": graph.number_of_edges()}}

