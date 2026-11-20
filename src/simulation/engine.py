"""D004 discrete-event clock, physical road motion and versioned vehicle programs."""
from collections import deque
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from fractions import Fraction
from heapq import heappop, heappush
from itertools import count
from pathlib import Path
from types import MappingProxyType
from typing import Deque, Hashable, Mapping, Optional, Tuple
from uuid import uuid4

from src.common.timebase import event_second, exact_add, require_tick
from src.data.sources import write_json
from src.m2.passengers import Leg
from src.network.routing import RoadEdge, RoadRouter
from src.simulation.connections import check_connections
from src.simulation.events import (
    PRIORITY, Action, BatchSnapshot, CandidateRoute, EventKind, Motion,
    SimulationEvent, VehicleState,
)
from src.simulation.service import ServiceState


@dataclass
class _Vehicle:
    node: Optional[Hashable]
    capacity: int
    motion: Optional[Motion] = None
    tail: Deque[RoadEdge] = field(default_factory=deque)
    version: int = 0
    actions: Tuple[Action, ...] = ()


def _record(value):
    """Serialize frozen records and preserve the exact decimal failure evidence."""
    if is_dataclass(value):
        return {f.name: _record(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _record(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, frozenset)):
        return [_record(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


class Simulator:
    """Single-writer simulation. Only advance_to and same-tick commits execute work."""

    def __init__(self, router: RoadRouter, *, start_time=0, batch_seconds=10, failure_dir=None):
        self._router = router
        self._now = require_tick(start_time)
        self._origin = self._now
        self._batch_seconds = require_tick(batch_seconds)
        if not self._batch_seconds:
            raise ValueError("batch_seconds must be positive")
        self._version = 0
        self._vehicles = {}
        self._service = ServiceState()
        self._arrivals = {}
        self._queue = []
        self._seq = count()
        self._history = []
        self._status = "RUNNING"
        self._failure_dir = Path(failure_dir or "outputs/p1_failures")
        self.failure_path = None

    @property
    def now(self):
        return self._now

    @property
    def router(self):
        return self._router

    @property
    def status(self):
        return self._status

    @property
    def history(self):
        return tuple(self._history)

    def _running(self):
        if self.status == "FAILED":
            raise RuntimeError("Simulation is FAILED")

    def _future(self, tick):
        self._running()
        if require_tick(tick) < self.now:
            raise ValueError(f"Time {tick} precedes simulator clock {self.now}")

    def _schedule(self, tick, kind, object_id, *, vehicle_id=None, version=None, edge=None, physical_time=None):
        self._future(tick)
        event = SimulationEvent(tick, EventKind(kind), next(self._seq), object_id,
                                vehicle_id, version, edge, physical_time)
        heappush(self._queue, (tick, event.priority, event.seq, event))

    def add_vehicle(self, vehicle_id, node, capacity=4):
        self._running()
        if not isinstance(vehicle_id, str) or not vehicle_id or vehicle_id in self._vehicles:
            raise ValueError(f"Invalid or duplicate vehicle ID {vehicle_id!r}")
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("Vehicle capacity must be a positive integer")
        self._router.position(node)
        self._vehicles[vehicle_id] = _Vehicle(node, capacity)
        self._version += 1

    def schedule_arrival(self, request_id, at, load_request):
        """Register only a release envelope. Payload is read once at its true timestamp."""
        self._future(at)
        if request_id in self._arrivals or request_id in self._service.requests:
            raise ValueError(f"Duplicate request release {request_id!r}")
        if not callable(load_request):
            raise ValueError("Request release requires a deferred loader")
        self._arrivals[request_id] = load_request
        self._schedule(at, "ARRIVAL", request_id)

    def add_flight(self, flight):
        self._future(flight.depart_at)
        self._router.position(flight.origin)
        self._router.position(flight.destination)
        self._service.add_flight(flight)
        self._schedule(flight.depart_at, "DEPART", flight.flight_id)
        self._schedule(flight.arrive_at, "LAND", flight.flight_id)
        self._version += 1

    def accept(self, request_id, plan, bindings):
        """Bind one released request, its plan and all resources in one operation."""
        self._begin_decision()
        if isinstance(bindings, (str, bytes)):
            raise ValueError("Expected one binding per plan step")
        bindings = tuple(bindings)
        request = self._service.requests.get(request_id)
        if request is None:
            raise ValueError(f"Request {request_id!r} has not arrived")
        for leg, resource in zip(plan.legs, bindings):
            if isinstance(leg, Leg) and leg.mode == "CAR":
                if resource not in self._vehicles or self._vehicles[resource].capacity < request.party_size:
                    raise ValueError(f"Missing or insufficient vehicle {resource!r}")
                if self._router.shortest_time_s(leg.origin, leg.destination) != leg.nominal_s:
                    raise ValueError(f"Car leg {leg.leg_id} differs from static road travel time")
        followups = self._service.accept(request_id, plan, bindings, now=self.now)
        self._version += 1
        self._schedule_followups(followups)

    def _schedule_followups(self, followups):
        for at, kind, object_id in followups:
            self._schedule(at, kind, object_id)

    def position(self, vehicle_id):
        vehicle = self._vehicles[vehicle_id]
        motion = vehicle.motion
        if motion is None:
            return self._router.position(vehicle.node)
        edge = motion.edge
        start, end = self._router.position(edge.source), self._router.position(edge.target)
        if not edge.time_s:
            return end
        fraction = float((self.now - Fraction(motion.exact_start)) / Fraction(edge.time_s))
        fraction = max(0.0, min(1.0, fraction))
        return tuple(a + fraction * (b - a) for a, b in zip(start, end))

    def snapshot(self):
        self.advance_to(self.now)
        return self._snapshot()

    def _begin_decision(self):
        self._running()
        if (self.now - self._origin) % self._batch_seconds:
            raise ValueError(f"Time {self.now} is between decision batches")
        self.advance_to(self.now)

    def batch_snapshot(self):
        self._begin_decision()
        return self._snapshot()

    def _snapshot(self):
        service = self._service
        vehicles = {
            vid: VehicleState(vid, v.capacity, v.node, v.motion, tuple(v.tail), v.actions,
                              v.version, service.onboard(vid), service.occupancy(vid), self.position(vid))
            for vid, v in self._vehicles.items()
        }
        return BatchSnapshot(self.now, self._version, self._router.version, MappingProxyType(vehicles),
                             MappingProxyType(dict(service.requests)), MappingProxyType(dict(service.flights)),
                             MappingProxyType(dict(service.tasks)))

    def _validate_actions(self, candidate, vehicle, node, ready):
        """Check this vehicle's physical stops before switching the live version.

        Joint passenger connections and global scheduling are outside this primitive.
        """
        actions = candidate.actions
        for action in actions:
            self._future(action.time)
            if action.kind not in ("PICKUP", "DROPOFF", "DRIVE"):
                raise ValueError(f"Invalid vehicle action {action.kind!r}")
        actions = tuple(sorted(actions, key=lambda a: (a.time, PRIORITY[EventKind(a.kind)])))
        seen = set()
        onboard = set(self._service.onboard(candidate.vehicle_id))
        occupancy = self._service.occupancy(candidate.vehicle_id)
        for action in actions:
            if action.time < event_second(ready):
                raise ValueError(f"Vehicle still travelling at {action.time}")
            if action.kind == "DRIVE":
                duration = self._router.shortest_time_s(node, action.target)
                ready, node = exact_add(action.time, duration), action.target
                continue
            task = self._service.tasks.get(action.target)
            if (task is None or task.kind != action.kind or task.status != "PENDING"
                    or task.resource_id != candidate.vehicle_id or task.node != node
                    or task.task_id in seen):
                raise ValueError(f"Invalid pending {action.kind} at node {node!r}: {action.target!r}")
            req = self._service.requests[task.request_id]
            if action.kind == "PICKUP":
                if task.request_id in onboard or occupancy + req.party_size > vehicle.capacity:
                    raise ValueError("Candidate pickup violates vehicle occupancy")
                onboard.add(task.request_id)
                occupancy += req.party_size
            else:
                if task.request_id not in onboard:
                    raise ValueError("Candidate dropoff precedes pickup")
                onboard.remove(task.request_id)
                occupancy -= req.party_size
            seen.add(task.task_id)
        locked = {a.target for a in vehicle.actions if a.kind != "DRIVE"}
        if not locked.issubset(seen):
            raise ValueError("Replacement omits a scheduled committed task")
        if seen:
            known = {a.target: a.time for vid, v in self._vehicles.items()
                     for a in (actions if vid == candidate.vehicle_id else v.actions)
                     if a.kind != "DRIVE"}
            for rid in sorted({self._service.tasks[tid].request_id for tid in seen}):
                check_connections(self._service, rid, known, self.now)
        return actions

    def commit_route(self, candidate: CandidateRoute):
        """Atomically replace one remaining road program on a current snapshot."""
        self._begin_decision()
        if candidate.snapshot_version != self._version:
            raise ValueError("stale snapshot version")
        if candidate.vehicle_id not in self._vehicles:
            raise ValueError(f"Unknown vehicle {candidate.vehicle_id!r}")
        v = self._vehicles[candidate.vehicle_id]
        node = v.motion.edge.target if v.motion else v.node
        ready = v.motion.exact_end if v.motion else Decimal(self.now)
        edges = ()
        if candidate.destination is not None:
            edges = self._router.shortest_path_edges(node, candidate.destination)
            ready = exact_add(ready, self._router.shortest_time_s(node, candidate.destination))
            node = candidate.destination
        actions = self._validate_actions(candidate, v, node, ready)
        v.tail = deque(edges)
        v.version += 1
        v.actions = actions
        for action in actions:
            self._schedule(action.time, action.kind, action.target,
                           vehicle_id=candidate.vehicle_id, version=v.version)
        if v.motion is None:
            self._start_edge(candidate.vehicle_id, Decimal(self.now))
        self._version += 1

    def _start_edge(self, vehicle_id, start):
        vehicle = self._vehicles[vehicle_id]
        if not vehicle.tail:
            return
        edge = vehicle.tail.popleft()
        if vehicle.node != edge.source or vehicle.motion is not None:
            raise ValueError(f"Discontinuous road motion for {vehicle_id}")
        end = exact_add(start, edge.time_s)
        vehicle.motion = Motion(edge, start, end)
        vehicle.node = None
        # Physical completions deliberately carry no replaceable program version.
        self._schedule(event_second(end), "EDGE_END", vehicle_id, edge=edge, physical_time=end)

    def _edge_end(self, event):
        v = self._vehicles[event.object_id]
        motion = v.motion
        if (motion is None or motion.edge != event.edge
                or event_second(motion.exact_end) != event.time):
            raise ValueError("Road completion does not match active motion")
        v.node, v.motion = motion.edge.target, None
        self._start_edge(event.object_id, motion.exact_end)

    def _release(self, event):
        req = self._arrivals[event.object_id]()
        if req.request_id != event.object_id or req.submitted_at != self.now:
            raise ValueError("Request release envelope and payload disagree")
        if req.direct_road_time_s != self._router.shortest_time_s(req.origin, req.destination):
            raise ValueError("Request direct travel time differs from the directed road network")
        self._service.add_request(req)
        del self._arrivals[event.object_id]
        self._schedule(req.pickup_deadline + 1, "EXPIRE", req.request_id)
        self._schedule(int(req.final_deadline) + 1, "EXPIRE_FINAL", req.request_id)

    def _execute(self, e):
        if e.version is not None:
            v = self._vehicles[e.vehicle_id]
            if v.version != e.version:
                return False
            if not v.actions or v.actions[0] != Action(e.time, e.kind, e.object_id):
                raise ValueError("Event does not match remaining vehicle program")
        if e.kind == "EDGE_END":
            self._edge_end(e)
        elif e.kind == "ARRIVAL":
            self._release(e)
        elif e.kind == "DRIVE":
            if v.motion:
                raise ValueError("Cannot depart from a vehicle in motion")
            v.tail = deque(self._router.shortest_path_edges(v.node, e.object_id))
            self._start_edge(e.vehicle_id, Decimal(e.time))
        else:
            followups = self._service.execute(e.kind.value, e.object_id, e.time,
                                              lambda vid: self._vehicles[vid].node,
                                              lambda vid: self._vehicles[vid].capacity)
            self._schedule_followups(followups)
        if e.version is not None:
            v.actions = v.actions[1:]
        return True

    def advance_to(self, simulation_time_s: int):
        self._future(simulation_time_s)
        start = self.now
        while self._queue and self._queue[0][0] <= simulation_time_s:
            tick, _, _, event = heappop(self._queue)
            self._now = tick
            try:
                if self._execute(event):
                    self._history.append(event)
                    self._version += 1
            except Exception as exc:
                self._status = "FAILED"
                self._failure_dir.mkdir(parents=True, exist_ok=True)
                self.failure_path = self._failure_dir / f"failure-{uuid4().hex}.json"
                write_json(self.failure_path, {"status": self.status, "cause": str(exc),
                           "event": _record(event), "snapshot": _record(self._snapshot()),
                           "history": _record(self._history)})
                raise
        if start != simulation_time_s:
            self._version += 1
        self._now = simulation_time_s
