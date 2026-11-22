"""Offline audit of the archived P0 mappings; run from repository root with python -m analysis.p6_profile_real10.network."""
from pathlib import Path
from random import Random
import json

import networkx as nx
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

from src.network.road import RoadNodeIndex, read_road_graph

out = Path(__file__).resolve().parent
root = out.parents[1]
run = root / "outputs/20261121T151519Z-1ccd3331b2fa425e99cbfafde128809e"
net = root / "data/processed/p0_network"
req = root / "data/processed/p0_requests"


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(rows, name):
    pd.DataFrame(rows).to_csv(out / ("network_" + name + ".csv"), index=False)


def stats(values):
    return dict(mean_m=values.mean(), p50_m=values.median(), p95_m=values.quantile(.95),
                p99_m=values.quantile(.99), max_m=values.max())


graph = read_road_graph(net / "roads.json")
main = max(nx.strongly_connected_components(graph), key=len)
index = RoadNodeIndex(graph, main)
projection = Transformer.from_crs(4326, 32618, always_xy=True)
boundary = transform(projection.transform, shape(read(net / "boundary.geojson")["geometry"])).boundary
fleet = []
for vehicle, node in read(run / "manifest.json")["initial_fleet_nodes"].items():
    xy = graph.nodes[node]["position"]
    nearest, dist, _ = index.nearest([xy])
    fleet.append(dict(vehicle=vehicle, node=node, in_main=node in main, nearest_main=int(nearest[0]),
                      displacement_m=float(dist[0]), distance_to_boundary_m=Point(projection.transform(*xy)).distance(boundary),
                      forward_nodes=len(nx.descendants(graph,node))+1 if node not in main else None))
all_nodes, main_nodes = sorted(graph), sorted(main)
seed = read(run / "manifest.json")["seed_fleet"]
full_random, main_random = Random(seed), Random(seed)
for r in fleet:
    assert full_random.choice(all_nodes)==r["node"]
    r["redrawn_main_node"]=main_random.choice(main_nodes)
write(fleet,"fleet")
write(read(net / "air_audit.json")["stations"],"stations")

run_rows = pd.read_csv(run / "requests.csv")
official = pd.read_csv(req / "evaluation_full_graph_candidate.csv")
formal_ids = {name: pd.read_csv(req / (name+"_full_graph_candidate.csv"),usecols=["request_id"]).request_id for name in ("warmup","evaluation","tail")}
needed = set(pd.concat(formal_ids.values())) | set(run_rows.request_id)
keep = []
for part in pd.read_csv(req / "road_mapping_comparison.csv.gz", chunksize=100000):
    keep.append(part[part.request_id.isin(needed)])
mapping = pd.concat(keep).set_index("request_id",verify_integrity=True)
assert set(mapping.index)==needed and mapping.road_reachable.all()
groups = {"formal_eval":mapping.loc[official.request_id], "run_all":mapping.loc[run_rows.request_id]}
groups["formal_warmup"] = mapping.loc[formal_ids["warmup"]]
groups["formal_tail"] = mapping.loc[formal_ids["tail"]]
groups["formal_all"] = mapping.loc[pd.concat(formal_ids.values())]
for cohort, rows in run_rows.groupby("cohort"):
    groups["run_"+cohort.lower()]=mapping.loc[rows.request_id]

counts, distributions, sensitivity = [], [], []
for group, df in groups.items():
    move_o = df.origin_node.ne(df.largest_origin_node)
    move_d = df.destination_node.ne(df.largest_destination_node)
    counts.append(dict(group=group, requests=len(df), origin_changed=move_o.sum(),dest_changed=move_d.sum(),
                       both=(move_o & move_d).sum(),any_changed=(move_o | move_d).sum(), reachable_before=df.road_reachable.sum()))
    for endpoint, flag in (("origin",move_o), ("destination",move_d)):
        old = df[endpoint+"_snap_m"]
        new = df["largest_"+endpoint+"_snap_m"]
        for subset, include in (("all",old.notna()),("changed",flag)):
            for metric, arr in (("old",old),("new",new),("increase",new-old)):
                distributions.append(dict(group=group,endpoint=endpoint,subset=subset,metric=metric,
                                          n=int(include.sum()),**stats(arr[include])))
    old_max = df[["origin_snap_m","destination_snap_m"]].max(axis=1)
    new_max = df[["largest_origin_snap_m","largest_destination_snap_m"]].max(axis=1)
    for meters in (25,50,100,200,250,500,1000):
        sensitivity.append(dict(group=group,threshold_m=meters,old_above=(old_max>meters).sum(),
                                new_above=(new_max>meters).sum(),new_crossings=((old_max<=meters)&(new_max>meters)).sum()))
    if group in ("formal_eval","run_evaluation","formal_all"):
        changed = df.loc[move_o|move_d].copy()
        changed["max_endpoint_error_increase_m"] = (new_max-old_max).loc[changed.index]
        changed["original_road_m"] = [nx.shortest_path_length(graph,a,b,weight="length_m")
                                      for a,b in zip(changed.origin_node,changed.destination_node)]
        changed["resnapped_road_m"] = [nx.shortest_path_length(graph,a,b,weight="length_m")
                                      for a,b in zip(changed.largest_origin_node,changed.largest_destination_node)]
        changed.sort_values("max_endpoint_error_increase_m",ascending=False).to_csv(out / ("network_changed_"+group+".csv"))
write(counts,"counts")
write(distributions,"snap")
write(sensitivity,"sensitivity")