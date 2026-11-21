"""Realize fixed grid quotas on the directed road network (D010)."""
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from src.common.timebase import event_second, exact_add
from src.network.routing import RoadEdge, RoadID

from .grid import Cell


@dataclass(frozen=True)
class VehicleMove:
    vehicle_id: str
    source: Cell
    target: Cell
    origin: RoadID
    destination: RoadID
    edges: tuple[RoadEdge, ...]
    depart_at: int
    arrive_at: int
    exact_arrival_s: Decimal
    travel_time_s: Decimal
    distance_m: Decimal


@dataclass(frozen=True)
class RouteOption:
    vehicle_id: str
    destination: RoadID
    travel_time_s: Decimal


@dataclass(frozen=True)
class RouteFailure:
    source: Cell
    target: Cell
    quota: int
    vehicle_ids: tuple[str, ...]
    entry_nodes: tuple[RoadID, ...]
    reachable_options: tuple[RouteOption, ...]
    reason: str
    unfilled_slot: int
    assigned_options: tuple[tuple[Cell, RouteOption], ...]


class PathInvariantError(RuntimeError):
    """The road router's reconstructed path contradicts its reachable times."""


def _route_options(road, grid, vehicles, vehicle_ids, targets):
    by_node = defaultdict(list)
    for vid in vehicle_ids:
        by_node[vehicles[vid].node].append(vid)
    options = {cell: [] for cell in targets}
    entries = {cell: grid.entries[cell] for cell in targets if cell in grid.entries}
    if not entries:
        return {cell: () for cell in targets}
    for node, ids in by_node.items():
        times = road.shortest_times_from(node)
        for target, nodes in entries.items():
            entry = next((n for n in nodes if n in times), None)
            if entry is not None:
                options[target].extend(RouteOption(vid, entry, times[entry]) for vid in ids)
    return {cell: tuple(sorted(choices, key=lambda v: (v.travel_time_s, v.vehicle_id)))
            for cell, choices in options.items()}


def _assign_slot(slot, options, owners, matches):
    """Find an augmenting path, trying nearer-by-road vehicles first at each slot."""
    predecessor = {slot: None}
    queue = deque([slot])
    while queue:
        current = queue.popleft()
        for option in options[current[0]]:
            owner = owners.get(option.vehicle_id)
            if owner is None:
                while True:
                    owners[option.vehicle_id] = current
                    matches[current] = option
                    previous = predecessor[current]
                    if previous is None:
                        return True
                    current, option = previous
            if owner not in predecessor:
                predecessor[owner] = current, option
                queue.append(owner)
    return False


def realize(road, grid, snapshot, supply_ids, flow):
    """Find a complete assignment for cross-grid quotas, then fill local quotas."""
    cross_quotas = defaultdict(dict)
    for (source, target), quota in flow.items():
        if source != target:
            cross_quotas[source][target] = quota
    assignments, retained, remaining = [], [], []
    for source, ids in sorted(supply_ids.items()):
        quotas = cross_quotas[source]
        owners, matches = {}, {}
        if quotas:
            options = _route_options(road, grid, snapshot.vehicles, ids, quotas)
            for target, quota in sorted(quotas.items()):
                for index in range(quota):
                    if not _assign_slot((target, index), options, owners, matches):
                        entries, reachable = grid.entries.get(target, ()), options[target]
                        reason = ("NO_ROAD_ENTRY" if not entries else "NO_DIRECTED_PATH"
                                  if not reachable else "INSUFFICIENT_REACHABLE_VEHICLES")
                        previous = tuple((cell, matches[cell, slot]) for cell, slot in sorted(matches))
                        return (), (), (), RouteFailure(source, target, quota, ids, entries,
                                                       reachable, reason, index + 1, previous)
            assignments.extend((source, target, option) for (target, _), option in matches.items())
        parked = [vid for vid in ids if vid not in owners]
        local_quota = flow.get((source, source), 0)
        retained.extend(parked[:local_quota])
        remaining.extend(parked[local_quota:])

    assignments.sort(key=lambda row: (row[0], row[1], row[2].vehicle_id))
    moves = tuple(_make_move(road, snapshot, *row) for row in assignments)
    return moves, tuple(sorted(retained)), tuple(sorted(remaining)), None


def _make_move(road, snapshot, source, target, option):
    origin = snapshot.vehicles[option.vehicle_id].node
    edges = road.shortest_path_edges(origin, option.destination)
    node, duration = origin, Decimal(0)
    for edge in edges:
        if edge.source != node:
            raise PathInvariantError(f"Discontinuous rebalancing path for {option.vehicle_id!r}")
        node, duration = edge.target, exact_add(duration, edge.time_s)
    if not edges or node != option.destination or duration != option.travel_time_s:
        raise PathInvariantError(f"Rebalancing path and shortest road time disagree for {option.vehicle_id!r}")
    arrival = exact_add(snapshot.time, duration)
    return VehicleMove(option.vehicle_id, source, target, origin, option.destination, edges,
                       snapshot.time, event_second(arrival), arrival, duration,
                       sum((edge.length_m for edge in edges), Decimal(0)))
