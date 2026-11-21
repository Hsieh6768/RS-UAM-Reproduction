"""Committed M2 passenger and fixed-flight transitions for the P1 event engine."""

from dataclasses import replace
from types import MappingProxyType

from src.common.timebase import event_second, exact_add, require_tick
from src.m2.passengers import FlightInstance, Leg, Plan, Request, Task, WalkLink, identifier, task_id


class ServiceConflict(ValueError):
    """A proposed or physical action contradicts passenger or resource state."""


def lookup(mapping, key):
    try:
        return mapping[key]
    except KeyError as exc:
        raise ServiceConflict(f"Unknown service object {key!r}") from exc


def check_pickup(req, leg, now, first):
    if first and now > req.pickup_deadline:
        raise ServiceConflict(f"First boarding deadline missed for {req.request_id}")
    if leg.earliest_pickup is not None and now < leg.earliest_pickup:
        raise ServiceConflict(f"Too early for leg {leg.leg_id}")
    if leg.latest_pickup is not None and now > leg.latest_pickup:
        raise ServiceConflict(f"Latest pickup missed for leg {leg.leg_id}")


def check_dropoff(req, leg, now):
    if now > req.final_deadline:
        raise ServiceConflict(f"Final deadline missed for {req.request_id}")
    if isinstance(leg, Leg) and leg.latest_dropoff is not None and now > leg.latest_dropoff:
        raise ServiceConflict(f"Latest dropoff missed for leg {leg.leg_id}")


def pending_walk(req, now):
    if req.status == "ACCEPTED" and isinstance(req.plan.legs[req.step_index], WalkLink):
        step = req.plan.legs[req.step_index]
        return ((now, "WALK_START", task_id(req.request_id, step.leg_id, "WALK_START")),)
    return ()


def finish_step(req, task, now):
    step = req.plan.legs[req.step_index]
    check_dropoff(req, step, now)
    index = req.step_index + 1
    final = index == len(req.plan.legs)
    return replace(req, step_index=index, status="COMPLETED" if final else "ACCEPTED",
                   completed_at=now if final else None, node=step.destination,
                   ready_at=now, history=req.history + (task.task_id,))


class ServiceState:
    """Owns the committed state. Return events are enqueued by the simulator."""

    def __init__(self):
        self._requests = {}
        self._flights = {}
        self._tasks = {}
        self._onboard = {}

    @property
    def requests(self):
        return MappingProxyType(self._requests)

    @property
    def flights(self):
        return MappingProxyType(self._flights)

    @property
    def tasks(self):
        return MappingProxyType(self._tasks)

    def onboard(self, vehicle_id):
        return self._onboard.get(vehicle_id, ())

    def occupancy(self, vehicle_id):
        return sum(self._requests[rid].party_size for rid in self.onboard(vehicle_id))

    def fork(self):
        """Records and membership tuples are immutable. Isolate their owning tables."""
        other = ServiceState()
        other._requests = self._requests.copy()
        other._tasks = self._tasks.copy()
        other._flights = self._flights.copy()
        other._onboard = self._onboard.copy()
        return other

    def add_request(self, request: Request):
        if request.request_id in self._requests:
            raise ServiceConflict(f"Duplicate request {request.request_id}")
        if (request.status != "WAITING" or request.plan is not None or request.bindings != ()
                or request.history != () or request.step_index != 0 or request.node is not None
                or any(t is not None for t in (request.ready_at, request.accepted_at,
                                               request.first_pickup_at, request.completed_at))):
            raise ServiceConflict("Arrival contains service history")
        self._requests[request.request_id] = replace(request, node=request.origin, ready_at=request.submitted_at)

    def add_flight(self, flight: FlightInstance):
        if flight.flight_id in self._flights or flight.status != "SCHEDULED" or flight.reservations != ():
            raise ServiceConflict(f"Non-pristine or duplicate flight {flight.flight_id}")
        self._flights[flight.flight_id] = flight

    def accept(self, request_id, plan: Plan, bindings, *, now):
        """Bind the complete chain at one instant, after validating every step."""
        require_tick(now)
        req = lookup(self._requests, request_id)
        if not isinstance(plan, Plan) or plan.request_id != request_id or isinstance(bindings, (str, bytes)):
            raise ServiceConflict("Plan or binding sequence does not match request")
        bindings = tuple(bindings)
        if now < req.submitted_at:
            raise ServiceConflict("Cannot accept a future request")
        if req.plan is not None:
            if plan != req.plan or bindings != req.bindings:
                raise ServiceConflict(f"Cannot change accepted commitments for {request_id}")
            return ()
        if req.status != "WAITING" or now > req.pickup_deadline:
            raise ServiceConflict("Request no longer available for acceptance")
        if plan.legs[0].origin != req.origin or plan.legs[-1].destination != req.destination:
            raise ServiceConflict("Plan endpoints differ from request")
        if len(bindings) != len(plan.legs):
            raise ServiceConflict("Each step needs a resource binding (None for walking)")

        reservations, tasks = {}, {}
        previous = None
        earliest = now
        first_boarding = True
        for step, resource in zip(plan.legs, bindings):
            if isinstance(step, WalkLink):
                if resource is not None:
                    raise ServiceConflict("Walking cannot be assigned a vehicle or flight")
                kinds = ("WALK_START", "WALK_END")
                earliest = event_second(exact_add(earliest, step.nominal_s))
            else:
                identifier(resource)
                pickup = max(earliest, event_second(step.earliest_pickup or 0))
                if step.mode == "CAR":
                    kinds = ("PICKUP", "DROPOFF")
                    earliest = event_second(exact_add(pickup, step.nominal_s))
                else:
                    kinds = ("FLIGHT",)
                    flight = lookup(self._flights, resource)
                    if (resource in reservations or flight.status != "SCHEDULED" or flight.depart_at < pickup
                            or (flight.origin, flight.destination) != (step.origin, step.destination)
                            or (step.route_id is not None and step.route_id != flight.route_id)
                            or event_second(exact_add(flight.depart_at, step.nominal_s)) != flight.arrive_at
                            or flight.remaining_seats < req.party_size):
                        raise ServiceConflict(f"Incompatible fixed flight {resource} for {request_id}")
                    pickup, earliest = flight.depart_at, flight.arrive_at
                    reservations[resource] = replace(flight, reservations=tuple(sorted(
                        flight.reservations + ((request_id, req.party_size),))))
                check_pickup(req, step, pickup, first_boarding)
                first_boarding = False
            check_dropoff(req, step, earliest)
            for kind in kinds:
                tid = task_id(request_id, step.leg_id, kind)
                node = step.destination if kind in ("DROPOFF", "WALK_END") else step.origin
                tasks[tid] = Task(tid, request_id, step.leg_id, kind, resource, node, previous)
                previous = tid

        accepted = replace(req, status="ACCEPTED", plan=plan, bindings=bindings, accepted_at=now, ready_at=now)
        self._requests[request_id] = accepted
        self._flights.update(reservations)
        self._tasks.update(tasks)
        return pending_walk(accepted, now)

    def execute(self, kind, object_id, now, vehicle_node, vehicle_capacity):
        require_tick(now)
        if kind in ("EXPIRE", "EXPIRE_FINAL"):
            return self._expire(kind, object_id, now)
        if kind in ("DEPART", "LAND"):
            return self._flight_event(kind, object_id, now)
        task = lookup(self._tasks, object_id)
        if task.kind != kind or kind not in ("PICKUP", "DROPOFF", "WALK_START", "WALK_END"):
            raise ServiceConflict(f"Unexpected {kind} for {object_id}")
        req = lookup(self._requests, task.request_id)
        step = self._check_task(req, task, now, "PENDING")
        upcoming = ()
        onboard = None
        if kind in ("PICKUP", "DROPOFF"):
            if vehicle_node(task.resource_id) != task.node:
                raise ServiceConflict(f"Vehicle {task.resource_id} is not at task location {task.node}")
            occupants = self.onboard(task.resource_id)
            if kind == "PICKUP":
                self._check_ready(req, step, now)
                check_pickup(req, step, now, req.first_pickup_at is None)
                if req.request_id in occupants or self.occupancy(task.resource_id) + req.party_size > vehicle_capacity(task.resource_id):
                    raise ServiceConflict(f"Vehicle membership or capacity conflict at {object_id}")
                onboard = tuple(sorted(occupants + (req.request_id,)))
                updated = self._board(req, "ON_VEHICLE", now, task)
            else:
                if req.status != "ON_VEHICLE" or req.request_id not in occupants:
                    raise ServiceConflict(f"Passenger is not on the committed vehicle at {object_id}")
                pickup = self._tasks[task.predecessor].completed_at
                if now < exact_add(pickup, step.nominal_s):
                    raise ServiceConflict(f"Vehicle leg finishes before its travel lower bound at {object_id}")
                updated = finish_step(req, task, now)
                onboard = tuple(rid for rid in occupants if rid != req.request_id)
                upcoming = pending_walk(updated, now)
        elif kind == "WALK_START":
            self._check_ready(req, step, now)
            end = task_id(req.request_id, step.leg_id, "WALK_END")
            end_at = event_second(exact_add(now, step.nominal_s))
            check_dropoff(req, step, end_at)
            updated = replace(req, status="WALKING", node=None, history=req.history + (object_id,))
            upcoming = ((end_at, "WALK_END", end),)
        else:
            start_at = self._tasks[task.predecessor].completed_at
            if req.status != "WALKING" or now != event_second(exact_add(start_at, step.nominal_s)):
                raise ServiceConflict(f"Walk completion at incorrect time or location for {object_id}")
            updated = finish_step(req, task, now)
            upcoming = pending_walk(updated, now)

        self._requests[req.request_id] = updated
        self._tasks[object_id] = replace(task, status="COMPLETED", started_at=now, completed_at=now)
        if onboard is not None:
            self._onboard[task.resource_id] = onboard
        return upcoming

    def _expire(self, kind, request_id, now):
        req = lookup(self._requests, request_id)
        deadline = req.pickup_deadline if kind == "EXPIRE" else req.final_deadline
        if now <= deadline:
            raise ServiceConflict(f"Deadline is still inclusive for {request_id} at {now}")
        if req.status == "WAITING":
            self._requests[request_id] = replace(req, status="EXPIRED_UNSERVED")
        elif req.status not in ("COMPLETED", "EXPIRED_UNSERVED"):
            if kind == "EXPIRE_FINAL" or req.first_pickup_at is None:
                raise ServiceConflict(f"Accepted request {request_id} missed its {kind} deadline")
        return ()

    @staticmethod
    def _check_ready(req, step, now):
        if req.status != "ACCEPTED" or req.node != step.origin or req.ready_at > now:
            raise ServiceConflict(f"Passenger {req.request_id} is not ready at {step.origin}")

    def _check_task(self, req, task, now, expected):
        if task.status != expected or req.plan is None or req.step_index >= len(req.plan.legs):
            raise ServiceConflict(f"Invalid execution status for task {task.task_id}")
        step = req.plan.legs[req.step_index]
        if step.leg_id != task.leg_id or task.resource_id != req.bindings[req.step_index]:
            raise ServiceConflict(f"Task is outside the current committed leg: {task.task_id}")
        if task.predecessor is not None:
            prior = self._tasks[task.predecessor]
            if prior.completed_at is None or prior.completed_at > now:
                raise ServiceConflict(f"Unfinished predecessor at {task.task_id}")
        return step

    @staticmethod
    def _board(req, status, now, task):
        first = req.first_pickup_at if req.first_pickup_at is not None else now
        history = req.history + (task.task_id,) if status == "ON_VEHICLE" else req.history
        return replace(req, status=status, node=None, first_pickup_at=first, history=history)

    def _flight_event(self, kind, flight_id, now):
        flight = lookup(self._flights, flight_id)
        departing = kind == "DEPART"
        expected_time = flight.depart_at if departing else flight.arrive_at
        expected_status = "SCHEDULED" if departing else "AIRBORNE"
        if now != expected_time or flight.status != expected_status or flight.remaining_seats < 0:
            raise ServiceConflict(f"Fixed flight {flight_id} cannot {kind} at {now}")
        requests, tasks, upcoming = {}, {}, []
        # A shared reservation is a capacity relation. Validate all parties before
        # mutating any one of them, including when the last passenger is late.
        for rid, size in flight.reservations:
            req = self._requests[rid]
            if req.plan is None or req.step_index >= len(req.bindings) or size != req.party_size:
                raise ServiceConflict(f"Invalid reservation on {flight_id} for {rid}")
            step = req.plan.legs[req.step_index]
            if not isinstance(step, Leg) or step.mode != "FLIGHT" or req.bindings[req.step_index] != flight_id:
                raise ServiceConflict(f"Passenger {rid} has not reached flight {flight_id}")
            task = self._tasks[task_id(rid, step.leg_id, "FLIGHT")]
            self._check_task(req, task, now, "PENDING" if departing else "ACTIVE")
            if departing:
                self._check_ready(req, step, now)
                check_pickup(req, step, now, req.first_pickup_at is None)
                requests[rid] = self._board(req, "ON_FLIGHT", now, task)
                tasks[task.task_id] = replace(task, status="ACTIVE", started_at=now)
            else:
                if req.status != "ON_FLIGHT":
                    raise ServiceConflict(f"Passenger {rid} did not board flight {flight_id}")
                requests[rid] = finish_step(req, task, now)
                tasks[task.task_id] = replace(task, status="COMPLETED", completed_at=now)
                upcoming.extend(pending_walk(requests[rid], now))
        self._requests.update(requests)
        self._tasks.update(tasks)
        self._flights[flight_id] = replace(flight, status="AIRBORNE" if departing else "LANDED")
        return tuple(upcoming)
