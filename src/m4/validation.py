"""D014 independent schedule audit. Check supplied events without re-decoding."""
from collections import defaultdict
from fractions import Fraction
from math import ceil, isclose

from src.m2.passengers import WalkLink, task_id
from .baseline import record_failure


class ValidationError(ValueError):
    """A supposedly feasible schedule failed independent verification."""


def check(condition, reason):
    if not condition:
        raise ValidationError(reason)


def _original_appointments(road, car, clock, node):
    clock += sum(Fraction(edge.time_s) for edge in car.remaining_edges)
    if car.remaining_edges:
        node = car.remaining_edges[-1].target
    visits = {}
    for action in car.actions:
        if action.kind == "DRIVE":
            edges = road.shortest_path_edges(node, action.target)
            clock = action.time + sum(Fraction(e.time_s) for e in edges)
            node = action.target
        else:
            visits[action.target] = (ceil(clock), action.time)
            clock = action.time
    return visits


class ScheduleValidator:
    def __init__(self, decoder, failure_dir="outputs/p4a_failures"):
        self.batch = decoder
        self.failure_dir = failure_dir

    def validate(self, schedule):
        """Raise with a FAILED snapshot and candidate diagnostic on a violation."""
        try:
            self._validate(schedule)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            fault = exc if isinstance(exc, ValidationError) else ValidationError(str(exc))
            fault.failure_path = record_failure(self.failure_dir, self.batch.snapshot, fault, schedule)
            if fault is exc:
                raise
            raise fault from exc

    def _validate(self, out):
        batch, snap = self.batch, self.batch.snapshot
        check(out.batch_key is batch.batch_key and (out.version, out.time) == (snap.version, snap.time)
              and out.feasible, "Expected a feasible schedule on this snapshot")
        selected = set(out.encoding.choices)
        check(selected == set(out.trips) == set(out.completion), "Incomplete passenger tables")
        check(set(batch.fixed_choices) <= selected, "Omitted Type3 commitment")
        check(tuple(row.task.request_id for row in out.tasks) == out.encoding.sequence,
              "Task order differs from encoding")
        timelines = defaultdict(list)
        for row in out.tasks:
            check(row.task.request_id in selected and row.task.status == "PENDING" and
                  row.task.started_at is None and row.task.completed_at is None and
                  type(row.arrival) is int and type(row.at) is int,
                  f"Unexpected pending task or event time {row.task.task_id}")
            timelines[row.task.request_id].append(row)
        seats = {fid: dict(f.reservations) for fid, f in snap.flights.items()}
        for rid, choice in out.encoding.choices.items():
            req = snap.requests[rid]
            locked = rid in batch.fixed_choices
            plan = req.plan if locked else batch.catalog[rid, choice.plan_id]
            check(plan.plan_id == choice.plan_id and len(choice.resources) == plan.formal_leg_count,
                  f"Plan or resource count changed for {rid}")
            check((req.origin, req.destination) == (plan.legs[0].origin, plan.legs[-1].destination),
                  f"Plan endpoints differ from request {rid}")
            if locked:
                check(choice == batch.fixed_choices[rid], f"Changed fixed resources for {rid}")
            else:
                check(req.status == "WAITING" and req.submitted_at <= snap.time <= req.pickup_deadline,
                      f"Ineligible new request {rid}")
            check(len(out.trips[rid]) == len(plan.legs), f"Incomplete service chain for {rid}")
            ready = req.accepted_at if locked else snap.time
            first = True
            resource_iter = iter(choice.resources)
            remaining = iter(timelines[rid])
            for step, trip in zip(plan.legs, out.trips[rid]):
                pickup, end = trip.pickup, trip.dropoff
                check(type(pickup) is int and type(end) is int and
                      (trip.leg_id, trip.ready) == (step.leg_id, ready) and ready <= pickup <= end,
                      f"Broken passenger causality at {rid}/{step.leg_id}")
                if isinstance(step, WalkLink):
                    check(pickup == ready and end == ceil(pickup + Fraction(step.nominal_s)),
                          f"Invalid walk at {rid}/{step.leg_id}")
                else:
                    resource = next(resource_iter)
                    if not locked:
                        check(resource in batch.rows[rid, plan.plan_id, step.leg_id],
                              f"Resource outside sparse candidates for {rid}")
                    check((not first or pickup <= req.pickup_deadline) and
                          (step.earliest_pickup is None or step.earliest_pickup <= pickup) and
                          (step.latest_pickup is None or pickup <= step.latest_pickup) and
                          (step.latest_dropoff is None or end <= step.latest_dropoff),
                          f"Missed service window at {rid}/{step.leg_id}")
                    first = False
                    check(end >= pickup + Fraction(step.nominal_s), f"Travel bound at {rid}/{step.leg_id}")
                    if step.mode == "FLIGHT":
                        f = snap.flights[resource]
                        check((f.origin, f.destination, f.depart_at, f.arrive_at) ==
                              (step.origin, step.destination, pickup, end) and
                              (step.route_id is None or f.route_id == step.route_id)
                              and ceil(pickup + Fraction(step.nominal_s)) == end, f"Fixed flight {resource}")
                        if not locked:
                            check(rid not in seats[resource] and f.status == "SCHEDULED", f"Reused flight {resource}")
                            seats[resource][rid] = req.party_size
                        events = (("FLIGHT", step.origin, end),)
                    else:
                        events = (("PICKUP", step.origin, pickup), ("DROPOFF", step.destination, end))
                    for kind, node, at in events:
                        tid = task_id(rid, step.leg_id, kind)
                        historical = snap.tasks.get(tid) if locked else None
                        if historical and historical.status != "PENDING":
                            check((kind != "FLIGHT" or historical.started_at == pickup) and
                                  (historical.status == "ACTIVE" or historical.completed_at == at),
                                  f"Changed historical task {tid}")
                            continue
                        check(at >= snap.time, f"Pending task in the past {tid}")
                        row = next(remaining, None)
                        check(row is not None and (row.task.task_id, row.task.leg_id, row.task.kind,
                              row.task.node, row.task.resource_id, row.at) ==
                              (tid, step.leg_id, kind, node, resource, at), f"Wrong service event {tid}")
                        if kind == "FLIGHT":
                            check(row.begin == row.arrival == pickup and row.onboard_after is None,
                                  f"Wrong flight interval {tid}")
                ready = end
            check(next(remaining, None) is None and ready == out.completion[rid] and ready <= req.final_deadline,
                  f"Invalid final arrival or extra tasks for {rid}")
        check(out.flight_seats == seats and all(sum(s.values()) <= snap.flights[f].capacity for f, s in seats.items()),
              "Joint seat ledger mismatch")
        check(set(out.vehicles) == set(out.final_nodes) == set(snap.vehicles), "Vehicle tables incomplete")
        grouped = defaultdict(list)
        for row in out.tasks:
            if row.task.kind != "FLIGHT":
                grouped[row.task.resource_id].append(row)
        for vid, car in snap.vehicles.items():
            queue = out.vehicles[vid]
            check(tuple(grouped[vid]) == queue, f"Vehicle order mismatch for {vid}")
            node = car.motion.edge.target if car.motion else car.node
            clock = Fraction(car.motion.exact_end if car.motion else snap.time)
            visits = _original_appointments(batch.road, car, clock, node) if out.preserve_appointments else {}
            onboard = set(car.onboard)
            previous = None
            for row in queue:
                t = row.task
                edges = batch.road.shortest_path_edges(node, t.node)
                # Independently accumulate actual path edges as rational numbers.
                arrival = ceil(clock + sum(Fraction(e.time_s) for e in edges))
                if t.task_id in visits:
                    original_arrival, service = visits[t.task_id]
                    arrival = max(arrival, original_arrival)
                    check(row.at == service, f"Changed baseline appointment {t.task_id}")
                check(row.begin == clock and arrival == row.arrival <= row.at, f"Impossible drive to {t.task_id}")
                if t.kind == "PICKUP":
                    check(t.request_id not in onboard, f"Repeated pickup {t.task_id}")
                    onboard.add(t.request_id)
                else:
                    check(t.request_id in onboard and not (previous and previous.at == row.at and
                          previous.task.kind == "PICKUP"), f"Invalid dropoff order {t.task_id}")
                    onboard.remove(t.request_id)
                load = sum(snap.requests[r].party_size for r in onboard)
                check(0 <= load == row.onboard_after <= car.capacity, f"Wrong onboard count {t.task_id}")
                node, clock, previous = t.node, Fraction(row.at), row
            check(not onboard, f"Unfinished vehicle trips on {vid}")
            if not queue or out.preserve_appointments:
                if car.remaining_edges:
                    node = car.remaining_edges[-1].target
                for action in car.actions:
                    node = action.target if action.kind == "DRIVE" else snap.tasks[action.target].node
                if queue and queue[-1].task.task_id not in batch.appointments:
                    node = queue[-1].task.node
            check(out.final_nodes[vid] == node, f"Incorrect final node for {vid}")
        duration = sum(out.completion[r] - snap.requests[r].submitted_at for r in selected) / 60
        reward = len(selected) * batch.delta
        check(reward == out.f1 and isclose(duration, out.f2, abs_tol=1e-12) and
              isclose(batch.beta * reward - (1-batch.beta) * duration, out.v, abs_tol=1e-12),
              "Objective differs from passenger timeline")
