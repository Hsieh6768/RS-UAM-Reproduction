"""Manual P2 check: first three EVALUATION rows, one shared network, no simulation."""
import cProfile
import csv
from datetime import datetime
from itertools import islice
from time import perf_counter
from unittest.mock import patch

from src.common.config import load_config
from src.common.timebase import exact_add
from src.m1 import M1Config, M1Planner
from src.m1.pulse import TopK, reverse_pulse
from src.m2.passengers import Leg, Plan, Request
from src.network.prepared import PreparedNetwork


def measure(req, net, planner):
    # Profile only the small abstract search, leaving all road Dijkstra calls unprofiled.
    profile = cProfile.Profile()
    before = net.road._distance.cache_info()
    with patch("src.m1.planner.reverse_pulse", lambda *args: profile.runcall(reverse_pulse, *args)):
        start = perf_counter()
        plans = planner.generate(req, net)
        cold = perf_counter() - start
    after = net.road._distance.cache_info()
    start = perf_counter()
    hit = planner.generate(req, net)
    hit_us = (perf_counter() - start) * 1e6
    assert hit is plans and net.road._distance.cache_info() == after
    assert 1 <= len(plans) <= planner.config.top_k
    assert len({p.plan_id for p in plans}) == len(plans)
    assert [p.nominal_s for p in plans] == sorted(p.nominal_s for p in plans)
    for plan in plans:
        assert isinstance(plan, Plan) and plan.request_id == req.request_id and plan.formal_leg_count >= 1
        assert (plan.legs[0].origin, plan.legs[-1].destination) == (req.origin, req.destination)
        ready = req.submitted_at
        for step in plan.legs:
            end = exact_add(ready, step.nominal_s)
            if isinstance(step, Leg):
                assert ready == step.earliest_pickup <= step.latest_pickup and end <= step.latest_dropoff
            ready = end
        assert ready <= req.final_deadline

    if req.request_id == "554581":
        detour, = [p for p in plans if len(p.legs) == 2 and p.legs[0].destination == 5020830713]
        first = detour.legs[0]
        assert first.latest_pickup == 7620 and first.latest_dropoff < first.latest_pickup
        print("554581 first EP/LP/LD:", first.earliest_pickup, first.latest_pickup, first.latest_dropoff)
        print("554581 costs (s):", [str(p.nominal_s) for p in plans])

    stats = profile.getstats()
    visits = sum(s.callcount for s in stats if getattr(s.code, "co_name", None) == "search"
                 and s.code.co_filename == reverse_pulse.__code__.co_filename)
    offers = sum(s.callcount for s in stats if s.code == TopK.offer.__code__)
    misses = after.misses - before.misses
    print(req.request_id, f"{cold:.6f}", f"{hit_us:.1f}", after.hits - before.hits + misses,
          misses, visits, offers, len(plans))


def main():
    cfg = load_config("configs/paper_main.yaml")
    start = perf_counter()
    net = PreparedNetwork.from_config(cfg)
    print(f"network load {perf_counter()-start:.3f} s")
    planner = M1Planner(M1Config.from_config(cfg))
    origin = datetime.fromisoformat(cfg["simulation"]["warmup_start"])
    with open("data/processed/p0_requests/evaluation_full_graph_candidate.csv", newline="") as source:
        rows = list(islice(csv.DictReader(source), 3))
    assert [row["request_id"] for row in rows] == ["554581", "554583", "554598"]
    print("ID cold_s hit_us road_calls road_misses dfs_visits eligible_offers plans")
    for row in rows:
        u, v = int(row["origin_node"]), int(row["destination_node"])
        t = int((datetime.fromisoformat(row["request_time_local"]) - origin).total_seconds())
        req = Request(row["request_id"], t, u, v, int(row["passenger_count"]), net.road.shortest_time_s(u, v))
        measure(req, net, planner)


if __name__ == "__main__":
    main()
