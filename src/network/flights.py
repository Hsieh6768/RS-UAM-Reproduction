"""Directed air routes, co-located road access, and fixed flights (D001/D011)."""
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

from src.common.timebase import event_second, exact_add
from src.m2.passengers import FlightInstance


def _integer(value, field, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}, got {value!r}")
    return value


def _positive_decimal(value, field):
    if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or value <= 0:
        raise ValueError(f"{field} must be a positive finite number, got {value!r}")
    return Decimal(value)


@dataclass(frozen=True)
class AirRoute:
    route_id: str
    origin_id: str
    destination_id: str
    distance_m: Decimal
    nominal_time_s: Decimal
    headway_s: int


@dataclass(frozen=True)
class AirNetwork:
    station_nodes: Mapping[str, int]
    routes: Mapping[str, AirRoute]
    phase_s: int
    capacity: int

    @classmethod
    def from_p0(cls, path, *, phase_s: int = 0, capacity: int = 4,
                headway_s: Optional[int] = None) -> "AirNetwork":
        """Read P0 air_audit.json using the approved full-graph station mapping.

        An explicit headway verifies the configuration against every audited route.
        Phase is the time of service index zero in seconds from simulation start.
        """
        _integer(phase_s, "phase_s", 0)
        _integer(capacity, "capacity", 1)
        if headway_s is not None:
            _integer(headway_s, "headway_s", 1)
        audit = json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_float=Decimal)
        station_nodes = {}
        for station in audit["stations"]:
            name = station["vertiport_id"]
            if not isinstance(name, str) or not name or name in station_nodes:
                raise ValueError(f"Invalid or duplicate vertiport ID {name!r}")
            if station["transfer_time_seconds"] != 0:
                raise ValueError(f"Expected zero transfer time at {name}")
            station_nodes[name] = _integer(station["full_road_node"], "full_road_node", 1)

        routes = {}
        pairs = set()
        for row in audit["routes"]:
            key, origin, dest = row["route_id"], row["origin_id"], row["destination_id"]
            if not isinstance(key, str) or not key or key in routes:
                raise ValueError(f"Invalid or duplicate route ID {key!r}")
            if origin not in station_nodes or dest not in station_nodes or origin == dest:
                raise ValueError(f"Invalid endpoints for {key}: {origin!r} -> {dest!r}")
            if (origin, dest) in pairs or row["within_distance_limit"] is not True:
                raise ValueError(f"Duplicate directed route or distance limit exceeded: {key}")
            interval = _integer(row["headway_seconds"], "headway_seconds", 1)
            if headway_s is not None and interval != headway_s:
                raise ValueError(f"{key} has audited headway {interval}s, configured {headway_s}s")
            routes[key] = AirRoute(
                key, origin, dest, _positive_decimal(row["distance_m"], "distance_m"),
                _positive_decimal(row["nominal_time_s"], "nominal_time_s"), interval,
            )
            pairs.add((origin, dest))

        if len(station_nodes) != audit["stations_count"] or len(routes) != audit["directed_edges"]:
            raise ValueError("Station or route count differs from P0 air audit")
        return cls(MappingProxyType(dict(sorted(station_nodes.items()))),
                   MappingProxyType(dict(sorted(routes.items()))), phase_s, capacity)

    def flight(self, route_id: str, index: int) -> FlightInstance:
        """Materialize one fixed service; the simulator owns its live seat inventory."""
        _integer(index, "flight index", 0)
        route = self.routes[route_id]
        departure = self.phase_s + route.headway_s * index
        return FlightInstance(
            f"{route_id}:{index}", self.station_nodes[route.origin_id],
            self.station_nodes[route.destination_id], departure,
            event_second(exact_add(departure, route.nominal_time_s)), self.capacity, route_id,
        )
