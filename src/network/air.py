from collections import Counter
from math import isfinite

from src.common.geo import distance_m


def audit_air(stations, routes, speed_mps, max_distance_m):
    positions = {}
    for station in stations:
        station_id = station["vertiport_id"]
        lon, lat = station["longitude"], station["latitude"]
        if (
            not isinstance(station_id, str) or not station_id or station_id in positions
            or not isfinite(lon) or not isfinite(lat) or abs(lon) > 180 or abs(lat) > 90
            or station["transfer_time_seconds"] != 0
        ):
            raise ValueError(f"Invalid or duplicate vertiport: {station}")
        positions[station_id] = (lon, lat)
    ids, pairs = set(), set()
    indegree, outdegree = Counter(), Counter()
    route_details = []
    for route in routes:
        route_id = route["route_id"]
        a, b = route["origin_id"], route["destination_id"]
        if a not in positions or b not in positions:
            raise ValueError(f"Air route has unknown endpoint: {route}")
        if not route_id or route_id in ids or (a, b) in pairs or a == b:
            raise ValueError(f"Empty, self-linked or duplicate route: {route}")
        ids.add(route_id)
        pairs.add((a, b))
        indegree[b] += 1
        outdegree[a] += 1
        length = float(distance_m([positions[a]], [positions[b]])[0])
        route_details.append({
            **route, "distance_m": length, "nominal_time_s": length / speed_mps,
            "within_distance_limit": 0 < length <= max_distance_m,
        })
    reciprocal = sum((b, a) in pairs for a, b in pairs)
    return {
        "stations_count": len(stations), "directed_edges": len(pairs),
        "reciprocal_pairs": reciprocal // 2, "unpaired_directed_edges": len(pairs) - reciprocal,
        "headway_seconds_counts": dict(sorted(Counter(r["headway_seconds"] for r in routes).items())),
        "stations": [
            {**station, "in_degree": indegree[station["vertiport_id"]],
             "out_degree": outdegree[station["vertiport_id"]]}
            for station in stations
        ],
        "routes": route_details,
    }
