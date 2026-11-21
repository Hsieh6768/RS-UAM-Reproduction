"""D003–D006. Convert one M4 result into executable P1 commitments."""
from collections import defaultdict

from src.common.timebase import event_second, exact_add
from src.m2.passengers import Leg, WalkLink, task_id
from src.m4.baseline import record_failure
from src.m4.encoding import committed_choice, pending_formal, task_chain
from src.simulation.connections import check_connections
from src.simulation.events import Action, CandidateRoute


class CommitError(ValueError):
    """The proposed final schedule cannot be published on the real world."""


def require(condition, message):
    if not condition:
        raise CommitError(message)


def _bind(stage, snapshot, plans, result):
    choices = result.best_solution.encoding.choices
    fixed = {rid for rid, typ in snapshot.request_types().items() if typ == 3}
    require(fixed <= choices.keys(), "Missing historical Type3 commitment")
    chains = {}
    for rid, choice in sorted(choices.items()):
        req = stage._service.requests[rid]
        if rid in fixed:
            require(choice == committed_choice(req), f"Changed Type3 assignment for {rid}")
            plan = req.plan
        else:
            require(req.status == "WAITING" and req.plan is None and
                    req.submitted_at <= snapshot.time <= min(req.pickup_deadline, req.final_deadline),
                    f"Request outside the current acceptance window: {rid}")
            matches = [p for p in plans.get(rid, ()) if p.plan_id == choice.plan_id and p.request_id == rid]
            require(len(matches) == 1, f"Missing or ambiguous M1 plan for {rid}")
            plan = matches[0]
            require(plan == result.plan_catalog.get((rid, plan.plan_id)), f"M1 plan changed for {rid}")
            require(len(choice.resources) == plan.formal_leg_count, f"Incomplete resources for {rid}")
            resources = iter(choice.resources)
            bindings = tuple(next(resources) if isinstance(step, Leg) else None for step in plan.legs)
            for step, vid in zip(plan.legs, bindings):
                if isinstance(step, Leg):
                    require(vid in result.candidate_resources.get((rid, plan.plan_id, step.leg_id), ()),
                            f"Resource outside the current M3 candidates for {rid}/{step.leg_id}")
            stage._accept(rid, plan, bindings)
        chain = []
        for expected in task_chain(plan, choice):
            actual = stage._service.tasks[expected.task_id]
            require((actual.request_id, actual.leg_id, actual.resource_id, actual.kind,
                     actual.node, actual.predecessor) ==
                    (rid, expected.leg_id, expected.resource_id, expected.kind,
                     expected.node, expected.predecessor), f"Changed historical task {actual.task_id}")
            chain.append(actual)
        chains[rid] = pending_formal(chain)
    return chains


def _car_program(snapshot, vid, rows):
    """The first approach continues the current physical edge at its exact end.

    Later drives start at the preceding integer service event. Same-node stops
    need no DRIVE, allowing P1 to release seats before boarding at the same tick.
    """
    car = snapshot.vehicles[vid]
    node = car.motion.edge.target if car.motion else car.node
    approach = rows[0].task.node if rows[0].task.node != node else None
    actions = []
    for row in rows:
        if actions and node != row.task.node:
            actions.append(Action(actions[-1].time, "DRIVE", row.task.node))
        actions.append(Action(row.at, row.task.kind.value, row.task.task_id))
        node = row.task.node
    return CandidateRoute(snapshot.version, vid, approach, tuple(actions))


def _check_trip_rows(service, rid, trips, appointments):
    req = service.requests[rid]
    ready = req.accepted_at
    require(len(trips) == len(req.plan.legs), f"Incomplete trip timeline for {rid}")
    for step, resource, trip in zip(req.plan.legs, req.bindings, trips):
        if isinstance(step, WalkLink):
            pickup, dropoff = ready, event_second(exact_add(ready, step.nominal_s))
        elif step.mode == "FLIGHT":
            flight = service.flights[resource]
            pickup, dropoff = flight.depart_at, flight.arrive_at
        else:
            times = []
            for kind in ("PICKUP", "DROPOFF"):
                tid = task_id(rid, step.leg_id, kind)
                task = service.tasks[tid]
                times.append(appointments[tid] if task.status == "PENDING" else task.completed_at)
            pickup, dropoff = times
        require((trip.leg_id, trip.ready, trip.pickup, trip.dropoff) ==
                (step.leg_id, ready, pickup, dropoff), f"Trip differs from executable events for {rid}")
        ready = dropoff


def _install(stage, snapshot, catalog, result):
    out, state = result.best_solution, result.state
    require(result.source_snapshot is snapshot and (out.time, out.version) ==
            (snapshot.time, snapshot.version) and out.feasible, "M4 result belongs to a different snapshot")
    require(state.graph_version == snapshot.graph_version and state.batch_time == snapshot.time and
            state.choices == out.encoding.choices, "M4 state does not describe this schedule")
    selected = out.encoding.choices
    require(selected.keys() == out.completion.keys() == out.trips.keys(), "Incomplete passenger schedule")
    chains = _bind(stage, snapshot, catalog, result)
    pending = {rid: iter(tasks) for rid, tasks in chains.items()}
    grouped = defaultdict(list)
    appointments = {}
    ids = []
    for row in out.tasks:
        task = row.task
        require(task == next(pending[task.request_id], None), f"Unexpected pending task {task.task_id}")
        require(type(row.at) is int and row.at >= snapshot.time, f"Invalid task time for {task.task_id}")
        ids.append(task.task_id)
        if task.kind == "FLIGHT":
            flight = stage._service.flights[task.resource_id]
            require(row.at == flight.arrive_at and row.begin == row.arrival == flight.depart_at,
                    f"Fixed flight schedule mismatch for {task.task_id}")
        else:
            grouped[task.resource_id].append(row)
            appointments[task.task_id] = row.at
    require(not any(next(chain, None) for chain in pending.values()), "Omitted pending formal task")
    require(tuple(ids) == state.remaining_task_ids and
            tuple(row.task.request_id for row in out.tasks) == out.encoding.sequence,
            "Search state/encoding disagrees with pending task order")
    require(out.vehicles.keys() == snapshot.vehicles.keys(), "Incomplete fleet schedule")
    for vid, rows in out.vehicles.items():
        require(tuple(grouped[vid]) == rows, f"Changed vehicle order for {vid}")
        load = snapshot.vehicles[vid].occupancy
        for row in rows:
            party = snapshot.requests[row.task.request_id].party_size
            load += party if row.task.kind == "PICKUP" else -party
            require(load == row.onboard_after and 0 <= load <= snapshot.vehicles[vid].capacity,
                    f"Incorrect passenger load on {vid}")
        if rows:
            candidate = _car_program(snapshot, vid, rows)
            # Keep the exact existing road program when the safe baseline
            # replays all appointments (including physical waits/approaches).
            saved = tuple(a for a in snapshot.vehicles[vid].actions if a.kind != "DRIVE")
            proposed = tuple(a for a in candidate.actions if a.kind != "DRIVE")
            if not (out.preserve_appointments and proposed == saved):
                stage._install_route(candidate, stage._vehicles[vid], joint=True)
    require(out.flight_seats.keys() == snapshot.flights.keys(), "Incomplete flight ledger")
    for fid, ledger in out.flight_seats.items():
        require(dict(stage._service.flights[fid].reservations) == ledger, f"Joint seat ledger mismatch on {fid}")
    for rid, predicted in out.completion.items():
        _check_trip_rows(stage._service, rid, out.trips[rid], appointments)
        finish = check_connections(stage._service, rid, appointments, snapshot.time)
        require(finish == predicted, f"Final service time differs for {rid}: {finish} != {predicted}")
    stage.search_state = state


def commit_passengers(sim, snapshot, plans_by_request, result, *, failure_dir=None):
    """Succeed as one single-writer world publication or retain every prior event and binding."""
    try:
        stage = sim.stage(snapshot)
        _install(stage, snapshot, plans_by_request, result)
        sim.publish(snapshot, stage)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        fault = CommitError(str(exc))
        fault.failure_path = record_failure(failure_dir or sim._failure_dir, snapshot, exc, result.best_solution)
        raise fault from exc
