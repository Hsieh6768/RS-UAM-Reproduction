"""P1 execution records and immutable planning inputs (D004)."""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Hashable, Mapping, Optional, Tuple
from types import MappingProxyType

from src.common.timebase import event_second, exact_add, require_tick
from src.network.routing import RoadEdge
from src.m2.passengers import FlightInstance, Request, Task


class EventKind(str, Enum):
    EDGE_END = "EDGE_END"
    LAND = "LAND"
    WALK_END = "WALK_END"
    DROPOFF = "DROPOFF"
    WALK_START = "WALK_START"
    PICKUP = "PICKUP"
    DEPART = "DEPART"
    DRIVE = "DRIVE"
    ARRIVAL = "ARRIVAL"
    EXPIRE = "EXPIRE"
    EXPIRE_FINAL = "EXPIRE_FINAL"


# DECISION: completing physical movement frees locations/capacity first.
# Sequence numbers order peers. A newly caused event follows its cause.
PRIORITY = {kind: index for index, kind in enumerate(EventKind)}


@dataclass(frozen=True)
class SimulationEvent:
    time: int
    kind: EventKind
    seq: int
    object_id: Hashable
    vehicle_id: Optional[str] = None
    version: Optional[int] = None
    edge: Optional[RoadEdge] = None
    physical_time: Optional[Decimal] = None

    @property
    def priority(self):
        return PRIORITY[self.kind]


@dataclass(frozen=True)
class Action:
    """DRIVE targets a destination node; PICKUP/DROPOFF target stable task IDs."""
    time: int
    kind: str
    target: Hashable


@dataclass(frozen=True)
class CandidateRoute:
    snapshot_version: int
    vehicle_id: str
    destination: Optional[Hashable] = None
    actions: Tuple[Action, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "actions", tuple(self.actions))


@dataclass(frozen=True)
class Motion:
    edge: RoadEdge
    exact_start: Decimal
    exact_end: Decimal


@dataclass(frozen=True)
class VehicleState:
    vehicle_id: str
    capacity: int
    node: Optional[Hashable]
    motion: Optional[Motion]
    remaining_edges: Tuple[RoadEdge, ...]
    actions: Tuple[Action, ...]
    route_version: int
    onboard: Tuple[str, ...]
    occupancy: int
    position: Tuple[float, float]


@dataclass(frozen=True)
class BatchSnapshot:
    time: int
    version: int
    graph_version: str
    vehicles: Mapping[str, VehicleState]
    requests: Mapping[str, Request]
    flights: Mapping[str, FlightInstance]
    tasks: Mapping[str, Task]

    def remaining_route(self, vehicle_id):
        """Current committed destination and unexecuted appointments for one vehicle."""
        vehicle = self.vehicles[vehicle_id]
        destination = vehicle.remaining_edges[-1].target if vehicle.remaining_edges else None
        return CandidateRoute(self.version, vehicle_id, destination, vehicle.actions)

    def road_arrival(self, vehicle_id, destination, router):
        """Earliest integral arrival if redirected now, preserving the active edge."""
        if router.version != self.graph_version:
            raise ValueError("Snapshot and road network versions differ")
        vehicle = self.vehicles[vehicle_id]
        if vehicle.motion is None:
            node, ready = vehicle.node, self.time
        else:
            node, ready = vehicle.motion.edge.target, vehicle.motion.exact_end
        return event_second(exact_add(ready, router.shortest_time_s(node, destination)))

    def request_types(self, previous_batch_time=None):
        """PAPER §3.3. A boundary release belongs to the batch ending at that boundary."""
        if previous_batch_time is not None and require_tick(previous_batch_time) > self.time:
            raise ValueError("Previous batch time exceeds snapshot time")
        types = {}
        for rid, req in self.requests.items():
            if req.status == "WAITING":
                types[rid] = 1 if previous_batch_time is None or req.submitted_at > previous_batch_time else 2
            elif req.status not in ("COMPLETED", "EXPIRED_UNSERVED"):
                types[rid] = 3
        return MappingProxyType(types)
