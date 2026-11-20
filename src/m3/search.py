"""Per-batch candidate screening (paper §5.2 Algorithm 3, D009)."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from fractions import Fraction
from math import asin, cos, degrees, pi, radians, sin
from time import perf_counter
from types import MappingProxyType
from typing import Mapping, Optional

from rtree.index import Index

from src.common.geo import EARTH_RADIUS_M
from src.m2.passengers import FlightStatus, Leg, LegMode, Plan
from src.network.routing import RoadRouter
from src.simulation.events import BatchSnapshot

from .config import M3Config


LegKey = tuple[str, str, str]  # (request_id, plan_id, leg_id)
METRES_PER_DEGREE = EARTH_RADIUS_M * radians(1)


@dataclass(frozen=True)
class FixedCommitment:
    """Original plan and one binding per step, including None for walks."""

    plan: Plan
    bindings: tuple[Optional[str], ...]


@dataclass(frozen=True)
class M3Stats:
    index_build_s: float = field(compare=False)
    car_queries: int
    car_candidates_before_limit: Mapping[LegKey, int]
    car_legs_at_limit: int
    car_legs_truncated: int
    selected_cars_full: int
    selected_cars_insufficient_seats: int
    flight_queries: int
    flight_candidates: int
    total_s: float = field(compare=False)


@dataclass(frozen=True)
class SparseCandidates:
    """Ordinary LS rows and mandatory Type3 assignments for one snapshot."""

    snapshot_time: int
    snapshot_version: int
    by_leg: Mapping[LegKey, tuple[str, ...]]
    commitments: Mapping[str, FixedCommitment]
    stats: M3Stats


def _metric_coordinates(position):
    """Convert longitude and latitude to equatorial and meridional metres."""
    lon, lat = position
    return lon * METRES_PER_DEGREE, lat * METRES_PER_DEGREE


def _point_bounds(x, y):
    return x, y, x, y


def _car_candidates(index, cars, pickup, radius, party_size, limit):
    lon, lat = pickup
    lat_rad = radians(lat)
    cos_lat = cos(lat_rad)
    angle = radius / EARTH_RADIUS_M
    # A spherical cap has latitudinal half-height angle and a longitude
    # extremum at asin(sin(angle) / cos(lat)). Manhattan needs one longitude box.
    if angle >= pi / 2 - abs(lat_rad):
        left, right = -180.0, 180.0
    else:
        delta_lon = degrees(asin(min(1.0, sin(angle) / cos_lat)))
        left, right = lon - delta_lon, lon + delta_lon
    delta_lat = degrees(angle)
    bottom, top = lat - delta_lat, lat + delta_lat
    bounds = (*_metric_coordinates((left, bottom)), *_metric_coordinates((right, top)))
    ranks = []
    for slot in index.intersection(bounds):
        car = cars[slot]
        cx, cy = car.position
        # Adjacent longitude/latitude values can land on one floating-point
        # index coordinate. Keep the original inclusive bounding rectangle.
        if car.capacity >= party_size and left <= cx <= right and bottom <= cy <= top:
            # The haversine is monotone with spherical distance in metres.
            # Original coordinate differences preserve symmetry in ties.
            dlon = radians(cx - lon)
            dlat = radians(cy - lat)
            haversine = sin(dlat / 2) ** 2 + cos_lat * cos(radians(cy)) * sin(dlon / 2) ** 2
            ranks.append((haversine, car.vehicle_id, slot))
    ranks.sort()
    return tuple(cars[slot] for _, _, slot in ranks[:limit]), len(ranks)


def _flight_index(snapshot):
    groups = defaultdict(list)
    for flight in snapshot.flights.values():
        # P1 replays departure events at t before producing a batch snapshot.
        if flight.status == FlightStatus.SCHEDULED and flight.depart_at > snapshot.time:
            groups[flight.origin, flight.destination].append(flight)
    for group in groups.values():
        group.sort(key=lambda f: (f.depart_at, f.flight_id))
    return {od: (tuple(f.depart_at for f in group), group) for od, group in groups.items()}


def _flight_candidates(groups, leg, party_size):
    times, flights = groups.get((leg.origin, leg.destination), ((), ()))
    first = bisect_left(times, leg.earliest_pickup)
    last = bisect_right(times, leg.latest_pickup)
    return tuple(f.flight_id for f in flights[first:last]
                 if (leg.route_id is None or f.route_id == leg.route_id)
                 and f.arrive_at <= leg.latest_dropoff and f.remaining_seats >= party_size)


class M3Searcher:
    """Build resource candidates from P1 positions, P1 flights and P2 windows."""

    def __init__(self, road: RoadRouter, config: Optional[M3Config] = None):
        self._road = road
        self._config = config if config is not None else M3Config()

    def search(self, batch_snapshot: BatchSnapshot,
               plans_by_request: Mapping[str, tuple[Plan, ...]]) -> SparseCandidates:
        started = perf_counter()
        snapshot = batch_snapshot
        if snapshot.graph_version != self._road.version:
            raise ValueError("Snapshot and road network versions differ")
        candidates, commitments, counts = {}, {}, {}
        at_limit = truncated = full = insufficient = flight_queries = flight_count = 0
        limit = self._config.candidate_limit
        speed = self._config.vehicle_speed_mps

        build_started = perf_counter()
        cars = tuple(snapshot.vehicles.values())
        points = tuple(_metric_coordinates(car.position) for car in cars)
        entries = ((i, _point_bounds(*point), None) for i, point in enumerate(points))
        # An empty stream cannot be bulk loaded by libspatialindex.
        with closing(Index(entries) if points else Index()) as index:
            index_build_s = perf_counter() - build_started
            flights = _flight_index(snapshot)
            for rid, request_type in sorted(snapshot.request_types().items()):
                req = snapshot.requests[rid]
                if request_type == 3:
                    if req.plan is None or len(req.bindings) != len(req.plan.legs):
                        raise ValueError(f"Missing fixed commitment for request {rid!r}")
                    commitments[rid] = FixedCommitment(req.plan, req.bindings)
                    continue
                if not req.submitted_at <= snapshot.time <= req.pickup_deadline:
                    continue
                for plan in plans_by_request.get(rid, ()):
                    if plan.request_id != rid:
                        raise ValueError(f"Plan {plan.plan_id!r} belongs to a different request")
                    for leg in plan.legs:
                        if not isinstance(leg, Leg):
                            continue
                        key = (rid, plan.plan_id, leg.leg_id)
                        if key in candidates:
                            raise ValueError(f"Duplicate formal leg key {key!r}")
                        if leg.latest_pickup is None or (leg.mode == LegMode.FLIGHT and
                                (leg.earliest_pickup is None or leg.latest_dropoff is None)):
                            raise ValueError(f"Missing M1 time window for leg {key!r}")
                        if leg.mode == LegMode.CAR:
                            selected, count = (), 0
                            if leg.latest_pickup >= snapshot.time:
                                # Eq. (21) uses the original LP and never adjusts it using LD.
                                radius = speed * float(Fraction(leg.latest_pickup) - snapshot.time)
                                pickup = self._road.position(leg.origin)
                                selected, count = _car_candidates(index, cars, pickup, radius, req.party_size, limit)
                            candidates[key] = tuple(car.vehicle_id for car in selected)
                            counts[key] = count
                            at_limit += count >= limit
                            truncated += count > limit
                            full += sum(car.occupancy == car.capacity for car in selected)
                            insufficient += sum(car.capacity - car.occupancy < req.party_size for car in selected)
                        else:
                            rows = _flight_candidates(flights, leg, req.party_size)
                            candidates[key] = rows
                            flight_queries += 1
                            flight_count += len(rows)

        stats = M3Stats(index_build_s, len(counts), MappingProxyType(counts), at_limit, truncated,
                        full, insufficient, flight_queries, flight_count, perf_counter() - started)
        return SparseCandidates(snapshot.time, snapshot.version, MappingProxyType(candidates),
                                MappingProxyType(commitments), stats)
