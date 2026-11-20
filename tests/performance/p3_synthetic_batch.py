"""One bounded M3 timing sample with generated inputs and a fixed seed."""
from collections import Counter
from random import Random
from statistics import median

import networkx as nx

from src.m2.passengers import FlightInstance, Leg, Plan, Request
from src.m3 import M3Searcher
from src.network.routing import RoadRouter
from src.simulation.events import BatchSnapshot, VehicleState


def main():
    rng = Random(20261120)
    graph = nx.MultiDiGraph()
    positions = [(-74 + (i % 4) * .004, 40.73 + (i // 4) * .005) for i in range(8)]
    graph.add_nodes_from((i, {"position": point}) for i, point in enumerate(positions))
    road = RoadRouter(graph)
    vehicles, flights, requests, plans = {}, {}, {}, {}
    for i in range(96):
        node = i % 8
        lon, lat = positions[node]
        vid = f"v{i:03}"
        position = lon + rng.uniform(-.002, .002), lat + rng.uniform(-.002, .002)
        vehicles[vid] = VehicleState(vid, 4, node, None, (), (), 0, (), rng.randrange(5), position)
    for i in range(64):
        node = i % 8
        dep = 45 + 10 * (i // 8)
        fid = f"f{i:02}"
        reserved = i // 8 % 5
        flights[fid] = FlightInstance(fid, node, (node + 1) % 8, dep, dep + 15,
                                      reservations=(("history", reserved),) if reserved else ())
    for i in range(28):
        u = i % 8
        v = (u + 1) % 8
        rid = f"r{i:02}"
        requests[rid] = Request(rid, 0, u, v, i // 8 + 1, 80)
        plans[rid] = (Plan("car", rid, (Leg("1", "CAR", u, v, 45, 0, 50 + i * 2, 130),)),
                      Plan("air", rid, (Leg("1", "FLIGHT", u, v, 15, 45, 90 + u, 105 + u),)))

    snapshot = BatchSnapshot(40, 1, road.version, vehicles, requests, flights, {})
    searcher = M3Searcher(road)
    samples = [searcher.search(snapshot, plans) for _ in range(5)]
    result = samples[0]
    assert all(sample == result for sample in samples)
    counts = result.stats.car_candidates_before_limit
    flights_by_leg = (len(row) for key, row in result.by_leg.items() if key[1] == "air")
    print(f"Synthetic snapshot: {len(vehicles)} cars, {len(flights)} flights, {len(requests)} requests")
    for field in ("index_build_s", "total_s"):
        times_ms = [getattr(sample.stats, field) * 1000 for sample in samples]
        print(f"{field} (ms) min/median/max: {min(times_ms):.3f}/{median(times_ms):.3f}/{max(times_ms):.3f}")
    print("Car counts before cap:", dict(sorted(Counter(counts.values()).items())))
    print("Car counts after cap:", dict(sorted(Counter(len(result.by_leg[key]) for key in counts).items())))
    print("Flight counts:", dict(sorted(Counter(flights_by_leg).items())))
    stats = result.stats
    print(f"Car rows at limit: {stats.car_legs_at_limit}; truncated: {stats.car_legs_truncated}; "
          f"selected full: {stats.selected_cars_full}; selected short of seats: "
          f"{stats.selected_cars_insufficient_seats}; flight matches: {stats.flight_candidates}")


if __name__ == "__main__":
    main()
