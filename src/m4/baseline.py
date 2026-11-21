"""D004. Recover the remaining program from P1 history and vehicle appointments."""
from collections import defaultdict
from dataclasses import dataclass, replace
from graphlib import CycleError, TopologicalSorter
from heapq import heappop, heappush
from pathlib import Path
from uuid import uuid4

from src.common.timebase import event_second, exact_add
from src.data.sources import write_json
from src.m2.passengers import Leg, WalkLink
from src.network.routing import UnreachableRoad
from src.simulation.engine import _record
from .encoding import Encoding, committed_choice, expand, pending_formal, task_chain


class SnapshotError(ValueError):
    """Invalid committed history, physical state, or remaining appointments."""


class BaselineOrderRequired(SnapshotError):
    """P1 holds bindings but too little of the previous vehicle program to recover it."""


@dataclass(frozen=True)
class Appointment:
    arrival: int
    service: int


def record_failure(directory, snapshot, cause, schedule=None):
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / ("failure-" + uuid4().hex + ".json")
    write_json(path, {"status": "FAILED", "cause": str(cause), "snapshot": _record(snapshot),
                      "candidate": _record(schedule.encoding) if schedule is not None else None,
                      "schedule": None if schedule is None else {
                          "tasks": _record(schedule.tasks), "trips": _record(schedule.trips),
                          "vehicles": _record(schedule.vehicles), "final_nodes": _record(schedule.final_nodes),
                          "completion": _record(schedule.completion), "flight_seats": _record(schedule.flight_seats),
                          "repairs": _record(schedule.repairs), "failure": _record(schedule.failure),
                          "f1": schedule.f1, "f2": schedule.f2, "v": schedule.v}})
    return path


def require(condition, detail):
    if not condition:
        raise SnapshotError(detail)


def missing_shared_order(chains, appointments):
    parties = defaultdict(set)
    incomplete = set()
    for chain in chains.values():
        for t in pending_formal(chain):
            if t.kind in ("PICKUP", "DROPOFF"):
                parties[t.resource_id].add(t.request_id)
                if t.task_id not in appointments:
                    incomplete.add(t.resource_id)
    return any(len(parties[vid]) > 1 for vid in incomplete)


def committed_chain(snapshot, req, road):
    rid = req.request_id
    require(req.plan is not None and req.plan.request_id == rid and
            len(req.bindings) == len(req.plan.legs), f"Invalid fixed plan for {rid}")
    require(req.accepted_at is not None and req.submitted_at <= req.accepted_at <= snapshot.time
            and req.plan.legs[0].origin == req.origin and req.plan.legs[-1].destination == req.destination,
            f"Invalid accepted request {rid}")
    for step, binding in zip(req.plan.legs, req.bindings):
        if isinstance(step, WalkLink):
            require(binding is None, f"Walk has a resource for {rid}")
        elif step.mode == "CAR":
            require(binding in snapshot.vehicles, f"Missing locked vehicle for {rid}")
            try:
                duration = road.shortest_time_s(step.origin, step.destination)
            except UnreachableRoad as exc:
                raise SnapshotError(f"Unreachable locked leg {rid}/{step.leg_id}") from exc
            require(step.nominal_s == duration, f"Changed nominal road time for {rid}/{step.leg_id}")
        else:
            require(binding in snapshot.flights, f"Missing locked flight for {rid}")
            flight = snapshot.flights[binding]
            require(dict(flight.reservations).get(rid) == req.party_size and
                    (flight.origin, flight.destination) == (step.origin, step.destination) and
                    (step.route_id is None or step.route_id == flight.route_id) and
                    event_second(exact_add(flight.depart_at, step.nominal_s)) == flight.arrive_at,
                    f"Broken flight commitment for {rid}/{binding}")

    chain = []
    for template in task_chain(req.plan, committed_choice(req)):
        task = snapshot.tasks.get(template.task_id)
        require(task is not None and replace(task, status="PENDING", started_at=None,
                                             completed_at=None) == template,
                f"Missing or changed committed task {template.task_id}")
        chain.append(task)
    history = tuple(t.task_id for t in chain if t.status == "COMPLETED")
    require(req.history == history and history == tuple(t.task_id for t in chain[:len(history)]),
            f"Non-prefix history for {rid}")
    ready = req.accepted_at
    first = None
    index = 0
    status = "ACCEPTED"
    node = req.origin
    last = ready
    for task in chain:
        if task.status == "PENDING":
            require(task.started_at is None and task.completed_at is None, f"Pending history for {task.task_id}")
            continue
        require(index < len(req.plan.legs) and req.plan.legs[index].leg_id == task.leg_id
                and task.started_at is not None and last <= task.started_at <= snapshot.time,
                f"Invalid historical start for {task.task_id}")
        step = req.plan.legs[index]
        if task.kind in ("PICKUP", "FLIGHT"):
            first = task.started_at if first is None else first
            require(first <= req.pickup_deadline and (step.earliest_pickup is None or task.started_at >= step.earliest_pickup)
                    and (step.latest_pickup is None or task.started_at <= step.latest_pickup),
                    f"Historical pickup window for {task.task_id}")
        if task.kind == "FLIGHT":
            flight = snapshot.flights[task.resource_id]
            require(task.started_at == flight.depart_at, f"Historical flight departure {task.task_id}")
        if task.status == "ACTIVE":
            require(task.kind == "FLIGHT" and flight.status == "AIRBORNE" and task.completed_at is None
                    and chain[len(history)] == task, f"Invalid active task for {rid}")
            status, node = "ON_FLIGHT", None
            continue
        require(task.started_at <= task.completed_at <= snapshot.time and
                (task.kind == "FLIGHT" or task.started_at == task.completed_at),
                f"Invalid historical finish for {task.task_id}")
        if task.kind in ("PICKUP", "WALK_START"):
            status = "ON_VEHICLE" if task.kind == "PICKUP" else "WALKING"
            if task.kind == "WALK_START":
                require(task.started_at == ready, f"Walk did not start at readiness for {task.task_id}")
            node = None
        else:
            if task.kind == "FLIGHT":
                require(task.completed_at == flight.arrive_at and flight.status == "LANDED",
                        f"Historical flight landing {task.task_id}")
            else:
                earliest = event_second(exact_add(last, step.nominal_s))
                require(task.completed_at == earliest if isinstance(step, WalkLink) else task.completed_at >= earliest,
                        f"Historical travel duration for {task.task_id}")
            require(task.completed_at <= req.final_deadline and
                    (not isinstance(step, Leg) or step.latest_dropoff is None or task.completed_at <= step.latest_dropoff),
                    f"Historical dropoff window for {task.task_id}")
            index += 1
            ready, node, status = task.completed_at, step.destination, "ACCEPTED"
        last = task.completed_at
    require((req.step_index, req.first_pickup_at, req.ready_at, req.status, req.node) ==
            (index, first, ready, status, node) and index < len(req.plan.legs), f"Passenger state/history mismatch for {rid}")
    if isinstance(req.plan.legs[index], WalkLink):
        require(status == "WALKING" and event_second(exact_add(last, req.plan.legs[index].nominal_s)) > snapshot.time,
                f"Overdue automatic walking event for {rid}")
    return tuple(chain)


def prepare_baseline(snapshot, sparse, road, remaining_sequence=None):
    fixed = {rid: snapshot.requests[rid] for rid, typ in sorted(snapshot.request_types().items()) if typ == 3}
    require(set(sparse.commitments) == set(fixed), "P3 and P1 fixed request sets differ")
    for rid, req in fixed.items():
        saved = sparse.commitments[rid]
        require((saved.plan, saved.bindings) == (req.plan, req.bindings), f"P3 commitment differs for {rid}")
    chains = {rid: committed_chain(snapshot, req, road) for rid, req in fixed.items()}
    expected = {task.task_id for chain in chains.values() for task in chain}
    for tid, task in snapshot.tasks.items():
        require((tid in expected or task.status == "COMPLETED") and task.request_id in snapshot.requests and
                (task.request_id not in fixed or tid in expected), f"Orphan commitment task {tid}")
    onboard = defaultdict(set)
    for rid, req in fixed.items():
        if req.status == "ON_VEHICLE":
            onboard[req.bindings[req.step_index]].add(rid)
    for fid, flight in snapshot.flights.items():
        expected = "SCHEDULED" if snapshot.time < flight.depart_at else (
            "AIRBORNE" if snapshot.time < flight.arrive_at else "LANDED")
        require(flight.flight_id == fid and flight.status == expected, f"Flight clock mismatch on {fid}")
        for rid, size in flight.reservations:
            req = snapshot.requests.get(rid)
            require(req is not None and req.plan is not None and req.party_size == size
                    and sum(isinstance(s, Leg) and s.mode == "FLIGHT" and binding == fid
                            for s, binding in zip(req.plan.legs, req.bindings)) == 1,
                    f"Orphan or duplicate reservation {rid}/{fid}")

    graph = TopologicalSorter()
    owners = {}
    appointments = {}
    for rid, chain in chains.items():
        previous = ()
        for task in pending_formal(chain):
            graph.add(task.task_id, *previous)
            owners[task.task_id] = rid
            previous = (task.task_id,)
    for vid, car in sorted(snapshot.vehicles.items()):
        require(vid == car.vehicle_id and sorted(car.onboard) == sorted(onboard[vid]) and
                car.occupancy == sum(fixed[r].party_size for r in onboard[vid]) <= car.capacity,
                f"Vehicle occupancy mismatch for {vid}")
        node, ready = car.node, snapshot.time
        if car.motion:
            m = car.motion
            require(ready < m.exact_end, f"Invalid active edge for {vid}: overdue road edge "
                    f"{m.edge.identity} due at {m.exact_end}, snapshot at {ready}")
            require(node is None and m.exact_start <= ready and
                    exact_add(m.exact_start, m.edge.time_s) == m.exact_end and
                    m.edge in road.parallel_edges(m.edge.source, m.edge.target), f"Invalid active edge for {vid}")
            node, ready = m.edge.target, m.exact_end
        require(node is not None, f"Missing vehicle node for {vid}")
        for edge in car.remaining_edges:
            require(node == edge.source and edge in road.parallel_edges(node, edge.target),
                    f"Invalid remaining road program for {vid}")
            node, ready = edge.target, exact_add(ready, edge.time_s)
        previous = ()
        for action in car.actions:
            require(action.time >= event_second(ready), f"Unreachable committed appointment {action.target}")
            arrival = event_second(ready)
            ready = action.time
            if action.kind == "DRIVE":
                try:
                    ready = exact_add(ready, road.shortest_time_s(node, action.target))
                except UnreachableRoad as exc:
                    raise SnapshotError(f"Unreachable committed drive for {vid}") from exc
                node = action.target
            else:
                tid = action.target
                task = snapshot.tasks.get(tid)
                require(tid in owners and tid not in appointments and task.resource_id == vid
                        and task.kind == action.kind and task.node == node, f"Invalid appointment {tid}")
                graph.add(tid, *previous)
                previous = (tid,)
                appointments[tid] = Appointment(arrival, action.time)
    try:
        graph.prepare()
    except CycleError as exc:
        raise SnapshotError(f"Cyclic committed vehicle and passenger queues: {exc}") from exc
    # P1 allows partial appointment programs. In unconstrained ties finish one
    # party first, giving explicit appointments priority over unbooked work.
    heap, order = [], []
    while graph.is_active():
        for tid in graph.get_ready():
            appt = appointments.get(tid)
            heappush(heap, (appt.service if appt else float("inf"), owners[tid], tid))
        _, rid, tid = heappop(heap)
        order.append(rid)
        graph.done(tid)
    if remaining_sequence is not None:
        order = tuple(remaining_sequence)
        tasks = expand(order, {rid: pending_formal(chain) for rid, chain in chains.items()})
        for vid, car in snapshot.vehicles.items():
            supplied = [t.task_id for t in tasks if t.kind != "FLIGHT" and t.resource_id == vid
                        and t.task_id in appointments]
            locked = [a.target for a in car.actions if a.kind != "DRIVE"]
            require(supplied == locked, f"Previous sequence changed existing appointments on {vid}")
    return chains, Encoding({rid: committed_choice(r) for rid, r in fixed.items()}, order), appointments
