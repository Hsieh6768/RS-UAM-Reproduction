"""Inspect PBF tags and adjacent original refs for a few saved boundary/bottleneck examples."""
from pathlib import Path
import json

import networkx as nx
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

from src.network.pbf import _blobs, _deltas, _fields, _varints
from src.network.road import read_road_graph

out = Path(__file__).resolve().parent
root = out.parents[1]
net = root / "data/processed/p0_network"
graph = read_road_graph(net / "roads.json")
boundary = shape(json.loads((net / "boundary.geojson").read_text(encoding="utf-8"))["geometry"])
proj = Transformer.from_crs(4326,32618,always_xy=True)
border = transform(proj.transform,boundary).boundary
vehicles = pd.read_csv(out / "network_fleet.csv")
witnesses = {}
for r in vehicles.loc[vehicles.forward_nodes<=53].itertuples():
    reachable = nx.descendants(graph,r.node)|{r.node}
    for n in sorted(reachable):
        if graph.out_degree(n)==0:
            witnesses.setdefault(n,[]).append(r.vehicle)

# Include the closest saved source/sink nodes to the polygon to inspect additional crossing types.
ends = [n for n in graph if not graph.in_degree(n) or not graph.out_degree(n)]
def dist(n):
    return Point(proj.transform(*graph.nodes[n]["position"])).distance(border)
for n in sorted(ends,key=dist)[:40]:
    witnesses.setdefault(n,[])
ways = {}
for n in witnesses:
    for a,b,data in [*graph.in_edges(n,data=True),*graph.out_edges(n,data=True)]:
        ways.setdefault(data["way_id"],set()).add(n)

rows = []
for kind, content in _blobs(root/"new-york-190101.osm.pbf"):
    if kind!=b"OSMData":
        continue
    block=list(_fields(content))
    strings = [v for _,v in _fields(next(v for k,v in block if k==1))]
    if b"highway" not in strings:
        continue
    for _,group in (x for x in block if x[0]==2):
        for k,obj in _fields(group):
            if k!=3:
                continue
            way_id=next(_fields(obj))[1]
            if way_id not in ways:
                continue
            fields=dict(_fields(obj))
            tags={strings[a].decode():strings[b].decode() for a,b in zip(_varints(fields[2]),_varints(fields[3]))}
            refs=list(_deltas(fields[8]))
            for n in sorted(ways.pop(way_id)):
                adjacent=set()
                for i,m in enumerate(refs):
                    if m==n:
                        adjacent.update(refs[max(i-1,0):i]+refs[i+1:i+2])
                rows.append(dict(node=n,initial_vehicles=" ".join(witnesses[n]),distance_to_border_m=dist(n),
                                 indegree=graph.in_degree(n),outdegree=graph.out_degree(n),way_id=way_id,
                                 original_adjacent_absent=" ".join(str(m) for m in sorted(adjacent) if m not in graph),
                                 name=tags.get("name"),highway=tags.get("highway"),bridge=tags.get("bridge"),
                                 tunnel=tags.get("tunnel"),oneway=tags.get("oneway")))
    if not ways:
        break
assert not ways
pd.DataFrame(rows).sort_values(["node","way_id"]).to_csv(out/"network_boundary.csv",index=False)

# Fetch only the missing adjacent refs' coordinates to verify literal inside/outside.
# Dense blocks store increasing IDs in this PBF; search by membership, independent of ordering.
from itertools import accumulate
from src.network.pbf import _signed

missing = {int(v) for row in rows for v in row["original_adjacent_absent"].split()}
coords = {}
for kind, content in _blobs(root/"new-york-190101.osm.pbf"):
    if kind!=b"OSMData":
        continue
    block=list(_fields(content))
    scalars={k:v for k,v in block if k>=17}
    gran=scalars.get(17,100)
    for _,group in (x for x in block if x[0]==2):
        for k,obj in _fields(group):
            if k!=2:
                continue
            dense=dict(_fields(obj))
            ids=list(_deltas(dense[1]))
            candidates=missing.intersection(ids)
            if not candidates:
                continue
            lat=list(_deltas(dense[8]))
            lon=list(_deltas(dense[9]))
            for n in candidates:
                i=ids.index(n)
                coords[n]=((lon[i]*gran+scalars.get(20,0))*1e-9,(lat[i]*gran+scalars.get(19,0))*1e-9)
                missing.remove(n)
    if not missing:
        break
assert not missing
extended=[]
for r in rows:
    for n in r["original_adjacent_absent"].split():
        x,y=coords[int(n)]
        extended.append(dict(node=r["node"],way=r["way_id"],adjacent_missing=n,lon=x,lat=y,
                             inside=boundary.covers(Point(x,y)),highway=r["highway"],name=r["name"]))
pd.DataFrame(extended).to_csv(out/"network_boundary_adjacent.csv",index=False)
