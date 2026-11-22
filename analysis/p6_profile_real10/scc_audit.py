"""Audit two archived E10 traces. Run from the repository root in ridesharing."""
import csv
import gzip
import hashlib
import json
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import ExitStack
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

import networkx as nx

OUT = Path(__file__).resolve().parent
ROAD = Path("data/processed/p0_network/roads.json")
RUNS = {"baseline": "20261121T095345Z", "profiling": "20261121T151519Z"}


def writer(stack, name, columns):
    f = stack.enter_context((OUT / ("scc_" + name + ".csv")).open("w", encoding="utf-8", newline=""))
    w = csv.writer(f)
    w.writerow(columns.split())
    return w.writerow


def audit():
    raw = ROAD.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    roads = json.loads(raw)
    graph = nx.DiGraph()
    graph.add_nodes_from(n[0] for n in roads["nodes"])
    graph.add_edges_from((e[0], e[1]) for e in roads["directed_edges"])
    comps = sorted(nx.strongly_connected_components(graph), key=lambda s: (-len(s), min(s)))
    comp = {n: i for i, s in enumerate(comps) for n in s}
    dag = nx.condensation(graph, comps)
    upstream = nx.ancestors(dag, 0)
    downstream = nx.descendants(dag, 0)
    def region(i):
        if i == 0:
            return "core"
        if i in upstream:
            return "upstream"
        if i in downstream:
            return "downstream"
        return "other"

    @lru_cache(None)
    def reach(i):
        return {i} | nx.descendants(dag, i)

    result = {"network_sha256": sha, "scc_count": len(comps), "largest_size": len(comps[0])}
    result["region_scc_counts"] = Counter(region(i) for i in dag)
    result["region_node_counts"] = Counter(region(i) for i in comp.values())
    with ExitStack() as stack:
        position = writer(stack, "positions", "run batch time vehicle last_completed_node edge_source edge_target source_scc target_scc available_scc position")
        totals = writer(stack, "batches", "run batch time cohort core_at_last_completed_node outside_at_last_completed_node core_at_available_node stationary in_edge_same_scc in_edge_cross_scc upstream_last downstream_last other_last")
        crossing = writer(stack, "crossings", "run event_second physical_end vehicle source target from_scc to_scc purpose onboard_request_ids next_pickup_id next_pickup_second")
        trip = writer(stack, "episodes", "run vehicle event time node scc request_id req_origin_scc req_dest_scc")
        fleet = writer(stack, "fleet", "run vehicle initial_node initial_scc reaches_core initial_reachable_nodes final_node final_scc final_reachable_nodes edges cross_edges pickups dropoffs outside_pickups outside_dropoffs m5_failure_mentions m5_failure_mentions_without_option post_exit_pickups post_exit_dropoffs initial_region final_region")
        od = writer(stack, "request_od", "run cohort final_state origin_core destination_core same_scc requests")
        block = writer(stack, "m5_mentions", "run batch vehicle available_scc reaches_core entries reachable_entries logged_option quota")
        for label, prefix in RUNS.items():
            (p,) = list(Path("outputs").glob(prefix + "-*"))
            manifest = json.loads((p / "manifest.json").read_text("utf-8"))
            assert manifest["road_network_sha256"] == sha
            init = manifest["initial_fleet_nodes"]
            req = {}
            od_counts = Counter()
            with (p / "requests.csv").open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    rid = r["request_id"]
                    a, b = (comp[int(r[k])] for k in ("mapped_origin", "mapped_dest"))
                    assert rid not in req and b in reach(a)
                    req[rid] = (a, b)
                    od_counts[(r["cohort"], r["state"], a == 0, b == 0, a == b)] += 1
            for key, count in sorted(od_counts.items()):
                od([label, *key, count])
            nodes = init.copy()
            edges = defaultdict(list)
            ends = defaultdict(list)
            changes = []
            counts = defaultdict(Counter)
            onboard = defaultdict(set)
            pickups = defaultdict(list)
            exit_time = {}
            with gzip.open(p / "vehicle_events.csv.gz", "rt", encoding="utf-8", newline="") as f:
                for e in csv.DictReader(f):
                    k, v, t = e["kind"], e["vehicle_id"], int(e["time"])
                    if k == "EDGE_END":
                        a, b = int(e["source"]), int(e["target"])
                        assert nodes[v] == a and graph.has_edge(a, b)
                        end, dur = Decimal(e["physical_time"]), Decimal(e["duration_s"])
                        start = end - dur
                        assert not ends[v] or ends[v][-1] <= start + Decimal("1e-18")
                        edges[v].append((start, a, b))
                        ends[v].append(end)
                        nodes[v] = b
                        counts[v]["edges"] += 1
                        if comp[a] != comp[b]:
                            counts[v]["cross"] += 1
                            purpose = e["road_purpose"]
                            assert (purpose == "LOADED") == bool(onboard[v])
                            changes.append((t, end, v, a, b, purpose, ",".join(sorted(onboard[v]))))
                            if comp[a] == 0:
                                exit_time[v] = t
                    elif k in ("PICKUP", "DROPOFF"):
                        rid = e["object_id"].split(":")[1]
                        n = nodes[v]
                        counts[v][k] += 1
                        if comp[n]:
                            counts[v]["out_" + k] += 1
                        if v in exit_time:
                            counts[v]["after_" + k] += 1
                        if k == "PICKUP":
                            pickups[v].append((t, rid))
                            assert rid not in onboard[v]
                            onboard[v].add(rid)
                        else:
                            onboard[v].remove(rid)
                        trip([label, v, k, t, n, comp[n], rid, *req[rid]])
            assert not any(onboard.values())
            for t, end, v, a, b, purpose, riders in changes:
                nxt = next(((str(tm), rid) for tm, rid in pickups[v] if tm >= t), ("", ""))
                crossing([label, t, end, v, a, b, comp[a], comp[b], purpose, riders, nxt[1], nxt[0]])
            spans = Counter()
            stats = Counter()
            by_phase = defaultdict(Counter)
            m5sets = Counter()
            core_hist = Counter()
            physical_hist = Counter()
            with (p / "batches.csv").open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    t, batch = int(r["batch_time"]), int(r["batch"])
                    available = {}
                    physical_core = 0
                    physical_regions = Counter()
                    batch_pos = Counter()
                    for v, n in init.items():
                        idx = bisect_right(ends[v], t)
                        last = edges[v][idx-1][2] if idx else n
                        a = b = None
                        ac = bc = comp[last]
                        state = "NODE"
                        if idx < len(edges[v]):
                            start, candidate_a, candidate_b = edges[v][idx]
                            if start <= t:
                                a, b = candidate_a, candidate_b
                                assert a == last
                                ac, bc = comp[a], comp[b]
                                state = "EDGE_CROSS" if ac != bc else "EDGE_SAME"
                        available[v] = bc
                        physical_core += int(comp[last] == 0)
                        physical_regions[region(comp[last])] += 1
                        batch_pos[state] += 1
                        spans[state] += 1
                        position([label, batch, t, v, last, a, b, ac, bc, bc, state])
                    avail_core = sum(i == 0 for i in available.values())
                    core_hist[avail_core] += 1
                    physical_hist[physical_core] += 1
                    totals([label, batch, t, r["cohort"], physical_core, len(init)-physical_core, avail_core,
                            batch_pos["NODE"], batch_pos["EDGE_SAME"], batch_pos["EDGE_CROSS"],
                            *[physical_regions[k] for k in ("upstream", "downstream", "other")]])
                    if r["m5_route_failure"]:
                        fail = json.loads(r["m5_route_failure"])
                        ids = fail["vehicle_ids"]
                        opts = {entry["vehicle_id"] for entry in fail["reachable_options"]}
                        entries = Counter(comp[int(n)] for n in fail["entry_nodes"])
                        phase = by_phase[r["cohort"]]
                        phase["failure"] += 1
                        reasons = set()
                        for v in ids:
                            i = available[v]
                            num = sum(c for j, c in entries.items() if j in reach(i))
                            counts[v]["mentions"] += 1
                            counts[v]["no_opt"] += int(v not in opts)
                            if not num:
                                reasons.add(v)
                            block([label,batch,v,i,0 in reach(i),sum(entries.values()),num,v in opts,fail["quota"]])
                        if reasons:
                            phase["with_topologically_blocked_vehicle"] += 1
                        initial_limited = {v for v in init if 0 not in reach(comp[init[v]])}
                        if reasons & initial_limited:
                            phase["with_initial_limited_blocked"] += 1
                        if reasons - initial_limited:
                            phase["with_other_blocked"] += 1
                        m5sets[",".join(sorted(reasons)) or "(none)"] += 1
                    stats["batches"] += 1
            for v,n in init.items():
                i = comp[n]
                j = comp[nodes[v]]
                c = counts[v]
                reach_nodes = lambda idx: sum(len(comps[k]) for k in reach(idx))
                fleet([label,v,n,i,0 in reach(i),reach_nodes(i),nodes[v],j,reach_nodes(j),
                       *[c[k] for k in ("edges","cross","PICKUP","DROPOFF","out_PICKUP","out_DROPOFF","mentions","no_opt","after_PICKUP","after_DROPOFF")],region(i),region(j)])
            result[label] = {"run_id": p.name, "positions": spans, "core_available_fleet_at_batches": core_hist,
                             "core_last_completed_fleet_at_batches": physical_hist,
                             "batch_count": stats["batches"], "m5_by_phase": by_phase,
                             "m5_topologically_blocked_vehicle_sets": m5sets, "requests": len(req),
                             "crossings": len(changes), "road_purpose_at_crossings": Counter(x[-2] for x in changes),
                             "edges":sum(map(len, ends.values())), "exits":exit_time,
                             "initial_regions": Counter(region(comp[n]) for n in init.values()),
                             "final_regions": Counter(region(comp[n]) for n in nodes.values())}
            result[label]["request_od_regions"] = Counter(region(a) + "->" + region(b) for a,b in req.values())
            assert sum(spans.values()) == stats["batches"] * len(init)
    (OUT / "scc_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    audit()
