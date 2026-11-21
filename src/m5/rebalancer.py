"""Read-only post-M4 rebalancing proposal. The caller owns publication."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from time import perf_counter, process_time
from types import MappingProxyType
from typing import Iterable, Mapping, Optional
from uuid import uuid4

from src.common.timebase import exact_add
from src.data.sources import write_json
from src.network.routing import RoadRouter, UnreachableRoad
from src.simulation.engine import _record
from src.simulation.events import BatchSnapshot, CandidateRoute, committed_car_ids

from .grid import Cell, RoadGrid, check_size
from .placement import PathInvariantError, RouteFailure, VehicleMove, realize
from .transport import GurobiConfig, TransportResult, solve_transport


@dataclass(frozen=True)
class M5Config:
    enabled: bool = True
    grid_size_m: float = 1000
    gurobi: GurobiConfig = GurobiConfig()

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("m5.enabled must be a boolean")
        check_size(self.grid_size_m)

    @classmethod
    def from_config(cls, config):
        values = config.get("m5", {})
        for key, supported in (
            ("solver", "gurobi"),
            ("cost", "grid_center_great_circle_distance"),
            ("allow_m4_interrupt_rebalance_after_current_edge", True),
            ("retarget_active_rebalancing_from_m5", False),
        ):
            if values.get(key, supported) != supported:
                raise ValueError(f"Unsupported m5.{key}: {values[key]!r}")
        return cls(enabled=values.get("enabled", True), grid_size_m=values.get("grid_size_m", 1000),
                   gurobi=GurobiConfig(**values.get("gurobi", {})))


@dataclass(frozen=True)
class RebalancePlan:
    status: str
    reason: Optional[str]
    batch_time: int
    snapshot_version: int
    grid: RoadGrid
    supply: Mapping[Cell, int]
    demand: Mapping[Cell, int]
    covered_demand: Mapping[Cell, int]
    uncovered_demand: Mapping[Cell, int]
    transport: Optional[TransportResult]
    moves: tuple[VehicleMove, ...]
    local_vehicle_ids: tuple[str, ...]
    remaining_vehicle_ids: tuple[str, ...]
    route_failure: Optional[RouteFailure]
    wall_s: float
    cpu_s: float
    placement_wall_s: float
    placement_cpu_s: float

    @property
    def supply_total(self):
        return sum(self.supply.values())

    @property
    def demand_total(self):
        return sum(self.demand.values())

    @property
    def supply_surplus(self):
        return self.supply_total - self.demand_total

    @property
    def road_distance_m(self):
        return sum((move.distance_m for move in self.moves), Decimal(0))

    @property
    def road_time_s(self):
        total = Decimal(0)
        for move in self.moves:
            total = exact_add(total, move.travel_time_s)
        return total

    @property
    def candidates(self):
        """P1 single-route records, all tied to the same post-M4 version."""
        return tuple(CandidateRoute(self.snapshot_version, move.vehicle_id, move.destination)
                     for move in self.moves)


class RebalanceInvariantError(RuntimeError):
    """Fatal inconsistency in a claimed optimum or in an already reachable road path."""

    def __init__(self, message, failure_path):
        super().__init__(message)
        self.failure_path = failure_path


def _invariant_error(directory, snapshot, transport, cause):
    path = Path(directory) / f"failure-{uuid4().hex}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"status": "FAILED", "cause": cause, "snapshot": _record(snapshot),
                      "transport": _record(transport)})
    return RebalanceInvariantError(cause, path)


class M5Rebalancer:
    def __init__(self, road: RoadRouter, config: M5Config = M5Config()):
        self._road = road
        self._config = config
        self._grid = RoadGrid.build(road, config.grid_size_m)
        self._cost_m = lru_cache(maxsize=16384)(self._grid.cost_m)

    def propose(self, committed_snapshot: BatchSnapshot, unmet_request_ids: Iterable[str],
                available_vehicle_ids: Iterable[str], *, failure_dir="outputs/p5_failures") -> RebalancePlan:
        """Read the effective post-M4 snapshot and current eligible ID sets.

        The vehicle allowlist excludes out-of-service vehicles. Pending passenger
        tasks and physical readiness are checked here against the snapshot.
        Request IDs may repeat (e.g. one per M1 option). The authoritative request
        status, release time, map-matched origin and deadlines come from P1.
        """
        started_wall, started_cpu = perf_counter(), process_time()
        snap, grid = committed_snapshot, self._grid
        if snap.graph_version != grid.graph_version:
            raise ValueError("Snapshot and road network versions differ")
        supply_ids, demand = self._counts(snap, unmet_request_ids, available_vehicle_ids)
        supply = {cell: len(ids) for cell, ids in sorted(supply_ids.items())}
        idle = tuple(sorted(vid for ids in supply_ids.values() for vid in ids))
        moves, local, remaining = (), (), idle
        transport = route_failure = None
        placement_wall = placement_cpu = 0.
        covered = Counter()
        if not self._config.enabled:
            status, reason = "SKIPPED", "DISABLED"
        elif not supply or not demand:
            status, reason = "SKIPPED", "NO_SUPPLY" if not supply else "NO_DEMAND"
        else:
            transport = solve_transport(supply, demand, self._cost_m, self._config.gurobi)
            if transport.status == "INVALID_SOLUTION":
                raise _invariant_error(failure_dir, snap, transport, transport.detail)
            if transport.status != "OPTIMAL":
                status, reason = "SOLVER_FAILED", transport.status
            else:
                path_wall, path_cpu = perf_counter(), process_time()
                try:
                    moves, local, remaining, route_failure = realize(
                        self._road, grid, snap, supply_ids, transport.flow)
                except (PathInvariantError, UnreachableRoad) as exc:
                    raise _invariant_error(failure_dir, snap, transport, str(exc)) from exc
                placement_wall = perf_counter() - path_wall
                placement_cpu = process_time() - path_cpu
                if route_failure is not None:
                    status, reason, remaining = "UNEXECUTABLE", route_failure.reason, idle
                else:
                    status, reason = "READY", None
                    for (_, target), count in transport.flow.items():
                        covered[target] += count
        uncovered = {cell: quantity - covered[cell] for cell, quantity in demand.items()
                     if quantity > covered[cell]}
        return RebalancePlan(status, reason, snap.time, snap.version, grid, MappingProxyType(supply),
                             MappingProxyType(demand), MappingProxyType(dict(sorted(covered.items()))),
                             MappingProxyType(uncovered), transport, moves, local, remaining, route_failure,
                             perf_counter() - started_wall, process_time() - started_cpu,
                             placement_wall, placement_cpu)

    def _counts(self, snapshot, request_ids, vehicle_ids):
        committed = committed_car_ids(snapshot.tasks.values())
        supply = defaultdict(list)
        for vid in sorted(set(vehicle_ids)):
            car = snapshot.vehicles.get(vid)
            if car is None:
                raise ValueError(f"Unknown vehicle ID in M5 input: {vid!r}")
            if car.parked_without_program and vid not in committed:
                supply[self._grid.node_cells[car.node]].append(vid)
        demand = Counter()
        for rid in set(request_ids):
            req = snapshot.requests.get(rid)
            if (req is not None and req.status == "WAITING" and req.plan is None
                    and req.submitted_at <= snapshot.time <= min(req.pickup_deadline, req.final_deadline)):
                demand[self._grid.node_cells[req.origin]] += 1
        return {cell: tuple(ids) for cell, ids in supply.items()}, dict(sorted(demand.items()))
