from dataclasses import replace
from decimal import Decimal
from math import acos, asin, cos, radians, sin
from random import Random

import networkx as nx

from src.m2.passengers import FlightInstance, Leg, Plan, Request
from src.m3 import M3Searcher
from src.network.routing import RoadRouter
from src.simulation.events import BatchSnapshot, VehicleState


def test_rtree_and_flight_search_match_independent_dense_reference():
    rng = Random(76455)
    graph = nx.MultiDiGraph()
    graph.add_nodes_from((i, {"position": point}) for i, point in enumerate(
        [(-74.00, 40.73), (-73.99, 40.73), (-74.00, 40.74), (-74.00, 40.75)]))
    road = RoadRouter(graph)
    vehicles = {}
    for i in range(48):
        center = graph.nodes[i % 4]["position"]
        lon = center[0] + rng.uniform(-.0017, .0017)
        lat = center[1] + rng.uniform(-.0014, .0014)
        vid = f"v{i:02}"
        capacity = rng.randint(1, 4)
        vehicles[vid] = VehicleState(vid, capacity, i % 4, None, (), (), 0, (),
                                    rng.randint(0, capacity), (lon, lat))
    vehicles["tie"] = replace(vehicles["v00"], vehicle_id="tie")
    all_plans, requests = {}, {}
    for i in range(24):
        rid = f"R{i}"
        u, v = i % 4, (i + 1) % 4
        requests[rid] = Request(rid, 0, u, v, 1 + i % 4, 100)
        all_plans[rid] = (
            Plan("car", rid, (Leg("1", "CAR", u, v, 5, 0, 46 + i * 2, 110),)),
            Plan("fly", rid, (Leg("1", "FLIGHT", u, v, 8, i + 42, i + 60, i + 69, str(u)),)),
        )
    flights = {}
    for i in range(96):
        u, v = i % 4, (i + 1) % 4
        depart = 38 + i // 2
        vid = f"f{i:02}"
        booked = rng.randint(0, 4)
        flights[vid] = FlightInstance(vid, u, v, depart, depart + rng.randint(8, 13),
                                      route_id=str(u if i % 5 else v),
                                      reservations=(("old", booked),) if booked else (),
                                      status="AIRBORNE" if depart <= 40 else "SCHEDULED")
    snap = BatchSnapshot(40, 7, road.version, vehicles, requests, flights, {})

    expected, dense, counts = {}, {}, {}
    columns = tuple(("CAR", v) for v in sorted(vehicles)) + tuple(("FLIGHT", f) for f in sorted(flights))
    for rid, plans in all_plans.items():
        party = requests[rid].party_size
        for plan in plans:
            leg, = plan.legs
            key = (rid, plan.plan_id, leg.leg_id)
            hits = []
            if leg.mode == "CAR":
                lon, lat = map(radians, graph.nodes[leg.origin]["position"])
                theta = float(Decimal("6.660896") * (leg.latest_pickup - 40)) / 6_371_009
                for vid, car in vehicles.items():
                    car_lon, car_lat = map(radians, car.position)
                    # In the miniature study area, test the distance to the
                    # vehicle's meridian separately from its latitude offset.
                    meridian_angle = asin(abs(cos(lat) * sin(car_lon - lon)))
                    if max(meridian_angle, abs(car_lat - lat)) <= theta and car.capacity >= party:
                        dot = sin(lat) * sin(car_lat) + cos(lat) * cos(car_lat) * cos(car_lon - lon)
                        hits.append((acos(max(-1.0, min(1.0, dot))), vid))
                counts[key] = len(hits)
                hits = sorted(hits)[:8]
            else:
                for fid, f in flights.items():
                    if (f.status == "SCHEDULED" and (f.origin, f.destination, f.route_id) ==
                            (leg.origin, leg.destination, leg.route_id) and f.depart_at > 40 and
                            leg.earliest_pickup <= f.depart_at <= leg.latest_pickup and
                            f.arrive_at <= leg.latest_dropoff and
                            sum(n for _, n in f.reservations) + party <= f.capacity):
                        hits.append((f.depart_at, fid))
                hits.sort()
            expected[key] = tuple(r for _, r in hits)
            dense[key] = [int(leg.mode == mode and rid in expected[key]) for mode, rid in columns]

    search = M3Searcher(road)
    result = search.search(snap, all_plans)
    assert dict(result.by_leg) == expected
    assert dict(result.stats.car_candidates_before_limit) == counts
    assert result.stats.car_legs_truncated == sum(n > 8 for n in counts.values()) > 0
    assert result.stats.car_legs_at_limit == sum(n >= 8 for n in counts.values())
    assert result.stats.flight_candidates == sum(sum(row) for key, row in dense.items() if key[1] == "fly") > 0
    for key, row in dense.items():
        mode = "CAR" if key[1] == "car" else "FLIGHT"
        assert [int(m == mode and rid in result.by_leg[key]) for m, rid in columns] == row
    shuffled = replace(snap, vehicles=dict(reversed(tuple(vehicles.items()))),
                       flights=dict(reversed(tuple(flights.items()))))
    assert search.search(shuffled, dict(reversed(tuple(all_plans.items())))).by_leg == result.by_leg
