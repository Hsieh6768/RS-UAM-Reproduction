"""Feasibility bounds for an existing commitment when a vehicle changes its appointments."""
from src.common.timebase import event_second, exact_add
from src.m2.passengers import WalkLink, task_id
from src.simulation.service import ServiceConflict, check_dropoff, check_pickup


def check_connections(service, request_id, appointments, now):
    """Propagate known times along one fixed chain without assigning any resources.

    A task not yet scheduled has only a lower bound. The runtime independently
    checks actual locations, completed predecessors and reservations at execution.
    """
    req = service.requests[request_id]
    ready = max(now, req.ready_at)
    first = req.first_pickup_at is None
    for index in range(req.step_index, len(req.plan.legs)):
        step = req.plan.legs[index]
        if isinstance(step, WalkLink):
            start = service.tasks[task_id(request_id, step.leg_id, "WALK_START")].completed_at
            ready = event_second(exact_add(ready if start is None else start, step.nominal_s))
        elif step.mode == "FLIGHT":
            flight = service.flights[req.bindings[index]]
            if flight.status != "AIRBORNE" and ready > flight.depart_at:
                raise ServiceConflict(f"Cannot reach locked flight {flight.flight_id} at {flight.depart_at}")
            check_pickup(req, step, flight.depart_at, first)
            ready = flight.arrive_at
            first = False
        else:
            pick_id = task_id(request_id, step.leg_id, "PICKUP")
            pick = service.tasks[pick_id].completed_at
            if pick is None:
                earliest = max(ready, event_second(step.earliest_pickup or 0))
                pick = appointments.get(pick_id, earliest)
                if pick < earliest:
                    raise ServiceConflict(f"Pickup precedes passenger readiness for {pick_id}")
            check_pickup(req, step, pick, first)
            earliest = max(now, event_second(exact_add(pick, step.nominal_s)))
            drop_id = task_id(request_id, step.leg_id, "DROPOFF")
            ready = appointments.get(drop_id, earliest)
            if ready < earliest:
                raise ServiceConflict(f"Dropoff precedes road travel bound for {drop_id}")
            first = False
        check_dropoff(req, step, ready)
    return ready
