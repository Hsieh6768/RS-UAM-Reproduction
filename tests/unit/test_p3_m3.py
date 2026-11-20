from dataclasses import replace
from math import asin, cos, degrees, radians, sin

import networkx as nx
import pytest

from src.common.config import load_config
from src.m2.passengers import FlightInstance, Leg, Plan, Request, WalkLink
from src.m3 import M3Config, M3Searcher
from src.network.routing import RoadRouter
from src.simulation.events import BatchSnapshot, VehicleState


EARTH_RADIUS = 6_371_009
SPEED = 14.9 * 1609.344 / 3600


def lon_lat(x, y=0):
    return degrees(x / EARTH_RADIUS), degrees(y / EARTH_RADIUS)


def vehicle(vid, x=0, y=0, capacity=4, occupancy=0):
    return VehicleState(vid, capacity, 0, None, (), (), 0, (), occupancy, lon_lat(x, y))


@pytest.fixture
def road():
    graph = nx.MultiDiGraph()
    for node in range(7):
        graph.add_node(node, position=lon_lat(node * 500))
    return RoadRouter(graph)


def snapshot(road, requests, vehicles=(), flights=(), time=10):
    return BatchSnapshot(time, time, road.version, {v.vehicle_id: v for v in vehicles},
                         {r.request_id: r for r in requests}, {f.flight_id: f for f in flights}, {})


def car_plan(rid, lp=20, ld=50, plan_id="p"):
    return Plan(plan_id, rid, (Leg("1", "CAR", 0, 1, 5, 0, lp, ld),))


def test_square_inclusive_boundary_corners_and_no_expansion(road):
    req = Request("r", 0, 0, 1, 1, 10)
    radius = 10 * SPEED
    snap = snapshot(road, [req], [vehicle("at"), vehicle("north", 0, radius),
                    vehicle("east", radius), vehicle("corner", radius * .9, radius * .9),
                    vehicle("outside", radius + .001), vehicle("south", 0, -radius - .001)])
    result = M3Searcher(road).search(snap, {"r": (car_plan("r"),)})
    assert result.by_leg[("r", "p", "1")] == ("at", "east", "north", "corner")
    assert result.stats.car_candidates_before_limit[("r", "p", "1")] == 4
    assert result.stats.car_legs_at_limit == 0


def test_zero_negative_and_original_lp_greater_than_ld(road):
    req = Request("r", 0, 0, 1, 1, 10)
    snap = snapshot(road, [req], [vehicle("at"), vehicle("near", .1), vehicle("applies-lp", 30)])
    plans = (car_plan("r", 10, plan_id="zero"), car_plan("r", "9.99999", plan_id="past"),
             car_plan("r", 20, 11, plan_id="wide"))
    result = M3Searcher(road).search(snap, {"r": plans})
    assert dict(result.by_leg) == {("r", "zero", "1"): ("at",), ("r", "past", "1"): (),
                                   ("r", "wide", "1"): ("at", "near", "applies-lp")}
    assert plans[2].legs[0].latest_pickup == 20 and plans[2].legs[0].latest_dropoff == 11


def test_square_at_manhattan_latitude():
    lon, lat = -74.0, 40.73
    graph = nx.MultiDiGraph()
    graph.add_node(0, position=(lon, lat))
    graph.add_node(1, position=(lon, lat + .001))
    road = RoadRouter(graph)
    theta = 10 * SPEED / EARTH_RADIUS
    angular_radius = degrees(theta)
    longitude_radius = degrees(asin(sin(theta) / cos(radians(lat))))
    positions = {"at": (lon, lat), "east": (lon + longitude_radius, lat),
                 "west": (lon - longitude_radius, lat), "north": (lon, lat + angular_radius),
                 "south": (lon, lat - angular_radius),
                 "corner": (lon + longitude_radius, lat + angular_radius),
                 "outside": (lon + longitude_radius * 1.0001, lat)}
    cars = [replace(vehicle(vid), position=point) for vid, point in positions.items()]
    snap = snapshot(road, [Request("r", 0, 0, 1, 1, 10)], cars)
    hits = M3Searcher(road).search(snap, {"r": (car_plan("r"),)}).by_leg[("r", "p", "1")]
    assert hits[0] == "at" and hits[-1] == "corner"
    assert set(hits) == set(positions) - {"outside"}


def test_projected_absolute_rounding_keeps_geographic_box_edge():
    lon, lat = -73.997448, 40.7122
    graph = nx.MultiDiGraph()
    graph.add_node(0, position=(lon, lat))
    graph.add_node(1, position=(lon, lat + .001))
    road = RoadRouter(graph)
    edge = degrees(asin(sin(81 * SPEED / EARTH_RADIUS) / cos(radians(lat))))
    cars = [replace(vehicle("boundary"), position=(lon + edge, lat)),
            replace(vehicle("outside"), position=(lon + edge + 1e-10, lat))]
    snap = snapshot(road, [Request("r", 0, 0, 1, 1, 10)], cars)
    result = M3Searcher(road).search(snap, {"r": (car_plan("r", lp=91),)})
    assert result.by_leg[("r", "p", "1")] == ("boundary",)


def test_spherical_box_contains_physical_in_circle_point_and_sorts_by_distance():
    lon, lat = -74.0, 40.73
    graph = nx.MultiDiGraph()
    graph.add_node(0, position=(lon, lat))
    graph.add_node(1, position=(lon, lat + .001))
    road = RoadRouter(graph)
    # The first point is 2797.5762867 m from the pickup, just inside the
    # 2797.57632 m circle and outside the linearized longitude half-width.
    positions = {"inside-edge": (-73.96679940316993, 40.73000475627573),
                 "north-east": (-73.9881323715113, 40.73899320335492),
                 "north": (-74.0, 40.74271800832047)}
    cars = [replace(vehicle(vid), position=point) for vid, point in positions.items()]
    snap = snapshot(road, [Request("r", 0, 0, 1, 1, 10)], cars)
    plan = car_plan("r", lp=430)
    assert M3Searcher(road).search(snap, {"r": (plan,)}).by_leg[("r", "p", "1")] == (
        "north-east", "north", "inside-edge")


def test_equal_distance_at_manhattan_latitude_breaks_tie_by_id():
    lon, lat = -74.0, 40.73
    graph = nx.MultiDiGraph()
    graph.add_node(0, position=(lon, lat))
    graph.add_node(1, position=(lon, lat + .001))
    road = RoadRouter(graph)
    delta = 2 / 65536  # Both offsets are exactly representable at this longitude.
    cars = [replace(vehicle("z-west"), position=(lon - delta, lat)),
            replace(vehicle("a-east"), position=(lon + delta, lat))]
    snap = snapshot(road, [Request("r", 0, 0, 1, 1, 10)], cars)
    searcher = M3Searcher(road, M3Config(vehicle_capacity=1, candidate_limit_multiplier=1))
    assert searcher.search(snap, {"r": (car_plan("r"),)}).by_leg[("r", "p", "1")] == ("a-east",)


def test_total_capacity_truncation_ids_and_current_occupancy(road):
    req = Request("r", 0, 0, 1, 2, 10)
    cars = [vehicle(f"v{i:02}", i + 1) for i in range(10)]
    cars += [vehicle("a-full", 1, occupancy=4), vehicle("b-loaded", 2, occupancy=3),
             vehicle("small", capacity=1), vehicle("c-loaded", 2, occupancy=1)]
    snap = snapshot(road, [req], reversed(cars))
    plans = {"r": (car_plan("r"),)}
    result = M3Searcher(road, M3Config.from_config(load_config("configs/base.yaml"))).search(snap, plans)
    key = ("r", "p", "1")
    assert result.by_leg[key] == ("a-full", "v00", "b-loaded", "c-loaded", "v01", "v02", "v03", "v04")
    assert result.stats.car_candidates_before_limit[key] == 13
    assert (result.stats.car_queries, result.stats.car_legs_at_limit,
            result.stats.car_legs_truncated, result.stats.selected_cars_full,
            result.stats.selected_cars_insufficient_seats) == (1, 1, 1, 1, 2)
    short = M3Searcher(road, M3Config(vehicle_capacity=2, candidate_limit_multiplier=1)).search(snap, plans)
    assert short.by_leg[key] == ("a-full", "v00")
    assert M3Searcher(road).search(snapshot(road, [req], cars), plans).by_leg == result.by_leg


def test_expired_requests_and_no_resources(road):
    req = Request("r", 0, 0, 1, 1, 10)
    expired = replace(req, request_id="expired", status="EXPIRED_UNSERVED")
    stale = replace(req, request_id="stale", pickup_tolerance_s=8)
    future = replace(req, request_id="future", submitted_at=11)
    snap = snapshot(road, [req, expired, stale, future])
    plans = {r.request_id: (car_plan(r.request_id),) for r in snap.requests.values()}
    result = M3Searcher(road).search(snap, plans)
    assert dict(result.by_leg) == {("r", "p", "1"): ()}
    assert result.stats.car_candidates_before_limit[("r", "p", "1")] == 0
    assert result.stats.flight_queries == 0


def test_flights_windows_direction_capacity_and_independent_new_requests(road):
    leg = Leg("1", "FLIGHT", 0, 1, 5, 20, 30, 35, "ab")
    requests = [Request(rid, 0, 0, 1, 2, 20) for rid in ("r", "s")]
    plans = {req.request_id: (Plan("p", req.request_id, (leg,)),) for req in requests}
    flight = FlightInstance("ep", 0, 1, 20, 25, route_id="ab", reservations=(("old", 2),))
    flights = [flight, replace(flight, flight_id="at-batch", depart_at=10),
               replace(flight, flight_id="early", depart_at=19),
               replace(flight, flight_id="late", depart_at=31, arrive_at=35),
               replace(flight, flight_id="late-arrival", depart_at=30, arrive_at=36),
               replace(flight, flight_id="wrong-origin", origin=2),
               replace(flight, flight_id="wrong-dest", destination=2),
               replace(flight, flight_id="reverse", origin=1, destination=0),
               replace(flight, flight_id="wrong-route", route_id="cd"),
               replace(flight, flight_id="already-departed", status="AIRBORNE"),
               replace(flight, flight_id="landed", status="LANDED"),
               replace(flight, flight_id="too-few", reservations=(("old", 3),)),
               replace(flight, flight_id="no-seats", reservations=(("old", 4),)),
               replace(flight, flight_id="lp-ld", depart_at=30, arrive_at=35)]
    snap = snapshot(road, requests, flights=flights)
    result = M3Searcher(road).search(snap, plans)
    assert dict(result.by_leg) == {("r", "p", "1"): ("ep", "lp-ld"),
                                   ("s", "p", "1"): ("ep", "lp-ld")}
    assert (result.stats.flight_queries, result.stats.flight_candidates) == (2, 4)
    assert snap.flights["ep"].reservations == (("old", 2),)
    assert sum(req.party_size for req in requests) > snap.flights["ep"].remaining_seats


def test_fixed_full_flight_and_outside_car_survive_ignore_replacement_plans(road):
    car = Leg("car", "CAR", 0, 1, 10, 0, 5, 10)
    flight = Leg("flight", "FLIGHT", 1, 2, 5, 10, 20, 25)
    plan = Plan("fixed", "old", (car, flight))
    old = Request("old", 0, 0, 2, 4, 20, status="ON_VEHICLE", plan=plan,
                  bindings=("outside", "f"), accepted_at=0)
    new = Request("new", 0, 0, 1, 1, 10)
    booked = FlightInstance("f", 1, 2, 20, 25, reservations=(("old", 4),))
    cars = [vehicle("outside", 100, occupancy=4)] + [vehicle(f"v{i}", i) for i in range(9)]
    snap = snapshot(road, [old, new], cars, [booked])
    searcher = M3Searcher(road)
    result = searcher.search(snap, {"old": (car_plan("old"),), "new": (car_plan("new"),)})
    assert dict(result.by_leg) == {("new", "p", "1"): tuple(f"v{i}" for i in range(8))}
    fixed = result.commitments["old"]
    assert fixed.plan is plan and fixed.bindings == ("outside", "f")
    in_square = dict(snap.vehicles, outside=vehicle("outside", 20, occupancy=4))
    cut = searcher.search(replace(snap, vehicles=in_square), {"new": (car_plan("new"),)})
    assert cut.commitments == result.commitments and cut.by_leg == result.by_leg
    assert cut.stats.car_candidates_before_limit[("new", "p", "1")] == 10
    only_fixed = searcher.search(replace(snap, time=30, flights={"f": replace(booked, status="LANDED")}), {})
    assert only_fixed.commitments == result.commitments and not only_fixed.by_leg
    with pytest.raises(TypeError):
        result.by_leg[("new", "p", "1")] = ()


def test_walks_have_no_resource_rows_and_do_not_rewrite_flight_window(road):
    req = Request("r", 0, 0, 6, 1, 100)
    plan = Plan("p", "r", (WalkLink("lead", 0, 1, 10, 1), WalkLink("lead2", 1, 2, 10, 1),
                Leg("car", "CAR", 2, 3, 5, 20, 40, 55), WalkLink("middle", 3, 4, 5, 1),
                WalkLink("transfer", 4, 4, 0, 1), Leg("air", "FLIGHT", 4, 5, 5, 30, 40, 50),
                WalkLink("tail", 5, 6, 5, 1)))
    flights = [FlightInstance("too-early", 4, 5, 29, 34), FlightInstance("static-ok", 4, 5, 30, 35)]
    snap = snapshot(road, [req], [vehicle("car", 1000)], flights, time=20)
    result = M3Searcher(road).search(snap, {"r": (plan,)})
    assert dict(result.by_leg) == {("r", "p", "car"): ("car",), ("r", "p", "air"): ("static-ok",)}
    assert snap.requests["r"].plan is None


def test_same_second_flight_is_not_available_to_a_new_batch(road):
    req = Request("r", 0, 0, 1, 1, 30)
    plan = Plan("p", "r", (Leg("air", "FLIGHT", 0, 1, 5, 10, 10, 15),))
    snap = snapshot(road, [req], flights=[FlightInstance("now", 0, 1, 10, 15)])
    assert M3Searcher(road).search(snap, {"r": (plan,)}).by_leg[("r", "p", "air")] == ()
