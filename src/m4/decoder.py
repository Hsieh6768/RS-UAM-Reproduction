"""Full, non-partitioned batch reference decoder (D002–D006)."""
from dataclasses import dataclass, field
from decimal import Decimal
from math import isfinite
from time import perf_counter, process_time

from src.common.timebase import event_second, exact_add
from src.m2.passengers import Leg, Node, Plan, Request, Task
from src.network.routing import UnreachableRoad
from .baseline import BaselineOrderRequired, SnapshotError, missing_shared_order, prepare_baseline, record_failure
from .encoding import Choice, Encoding, EncodingError, expand, frozen, pending_formal, task_chain
from .result import Conflict, DecodedSchedule, InitialSolutions, TimedTask, TripTime, objective


class _Infeasible(Exception):
    def __init__(self, rid, reason, detail):
        self.conflict = Conflict(rid, reason, str(detail))
        super().__init__(f"{rid}: {reason}: {detail}")


@dataclass
class _Journey:
    request: Request
    plan: Plan
    chain: tuple[Task, ...]
    ready: int
    index: int = 0
    first: bool = True
    pickup: int = 0
    trips: list = field(default_factory=list)

    @property
    def step(self):
        return self.plan.legs[len(self.trips)]

    def board(self, at):
        step, req = self.step, self.request
        if ((self.first and at > req.pickup_deadline) or
                (step.latest_pickup is not None and at > step.latest_pickup) or
                (step.earliest_pickup is not None and at < step.earliest_pickup)):
            raise _Infeasible(req.request_id, "pickup_window", (step.leg_id, at))
        self.first = False
        self.pickup = at

    def finish(self, at):
        step = self.step
        if at > self.request.final_deadline or (
                isinstance(step, Leg) and step.latest_dropoff is not None and at > step.latest_dropoff):
            raise _Infeasible(self.request.request_id, "dropoff_window", (step.leg_id, at))
        self.trips.append(TripTime(step.leg_id, self.ready, self.pickup, at))
        self.ready = at

    def advance_prefix_and_walks(self, snapshot):
        """Copy actual history, then run automatic walk/landing continuations."""
        while self.index < len(self.chain):
            t = self.chain[self.index]
            if t.resource_id is not None and t.status == "PENDING":
                break
            self.index += 1
            if t.kind == "WALK_START":
                self.pickup = self.ready if t.completed_at is None else t.completed_at
            elif t.kind == "WALK_END":
                self.finish(event_second(exact_add(self.pickup, self.step.nominal_s)))
            elif t.kind == "PICKUP":
                self.board(t.completed_at)
            elif t.kind == "DROPOFF":
                self.finish(t.completed_at)
            else:
                self.board(t.started_at)
                self.finish(snapshot.flights[t.resource_id].arrive_at)


@dataclass
class _Car:
    node: Node
    available: Decimal
    onboard: set
    load: int
    stops: list = field(default_factory=list)


class FullDecoder:
    """One evaluator per frozen P1 batch/M1 catalog/M3 search and scoring policy."""

    def __init__(self, road, snapshot, plans_by_request, candidates, *, beta=.7, delta=100,
                 failure_dir="outputs/p4a_failures", remaining_sequence=None):
        if (snapshot.graph_version != road.version or (snapshot.time, snapshot.version) !=
                (candidates.snapshot_time, candidates.snapshot_version)):
            raise ValueError("Stale road network or sparse batch candidates")
        if not (0 <= beta <= 1 and isfinite(delta) and delta > 0):
            raise ValueError("Invalid objective weights")
        self.road, self.snapshot = road, snapshot
        self.beta, self.delta = beta, delta
        self.batch_key = object()
        self.options = frozen({rid: tuple(plans) for rid, plans in plans_by_request.items()})
        self.catalog = {}
        for rid, plans in self.options.items():
            for plan in plans:
                if rid != plan.request_id or (rid, plan.plan_id) in self.catalog:
                    raise EncodingError(f"Ambiguous plan identity for {rid}")
                self.catalog[rid, plan.plan_id] = plan
        self.catalog = frozen(self.catalog)
        self.rows = frozen({key: tuple(values) for key, values in candidates.by_leg.items()})
        try:
            chains, baseline, appointments = prepare_baseline(snapshot, candidates, road, remaining_sequence)
            self.fixed_chains = frozen(chains)
            self.fixed_choices = baseline.choices
            self.appointments = frozen(appointments)
            self.safe_baseline = self._decode(baseline, preserve_appointments=True)
            if not self.safe_baseline.feasible:
                if remaining_sequence is None and missing_shared_order(chains, appointments):
                    raise BaselineOrderRequired(f"Partial P1 appointments; supply remaining_sequence: "
                                                f"{self.safe_baseline.failure}")
                raise SnapshotError(f"Infeasible remaining commitment: {self.safe_baseline.failure}")
        except SnapshotError as exc:
            exc.failure_path = record_failure(failure_dir, snapshot, exc)
            raise

    def selected_plan(self, rid, choice):
        if rid in self.fixed_choices:
            return self.snapshot.requests[rid].plan
        try:
            return self.catalog[rid, choice.plan_id]
        except KeyError as exc:
            raise EncodingError(f"Unknown M1 plan for {rid}: {choice.plan_id}") from exc

    def _resolve(self, enc):
        for rid, locked in self.fixed_choices.items():
            if enc.choices.get(rid) != locked:
                raise _Infeasible(rid, "changed_commitment", locked)
        chains, plans = {}, {}
        for rid, choice in sorted(enc.choices.items()):
            req = self.snapshot.requests.get(rid)
            if req is None or req.status in ("COMPLETED", "EXPIRED_UNSERVED"):
                raise EncodingError(f"Request outside solve set: {rid}")
            plan = self.selected_plan(rid, choice)
            plans[rid] = plan
            if rid in self.fixed_chains:
                chains[rid] = self.fixed_chains[rid]
                continue
            if (req.submitted_at > self.snapshot.time or self.snapshot.time > req.pickup_deadline or
                    (req.origin, req.destination) != (plan.legs[0].origin, plan.legs[-1].destination)):
                raise EncodingError(f"Request or plan outside admissible batch: {rid}")
            chain = tuple(task_chain(plan, choice))
            for step, res in zip((s for s in plan.legs if isinstance(s, Leg)), choice.resources):
                pool = self.snapshot.vehicles if step.mode == "CAR" else self.snapshot.flights
                if res not in pool or res not in self.rows.get((rid, plan.plan_id, step.leg_id), ()):
                    raise EncodingError(f"Invalid {step.mode.value} binding {res!r} for {rid}/{step.leg_id}")
            chains[rid] = chain
        pending = {rid: pending_formal(chain) for rid, chain in chains.items()}
        return plans, chains, expand(enc.sequence, pending)

    def decode(self, encoding: Encoding, *, preserve_appointments=False) -> DecodedSchedule:
        """Evaluate a trial, or replay this batch's safe encoding at its original appointments."""
        if not isinstance(encoding, Encoding):
            raise EncodingError("Expected an immutable two-layer Encoding")
        if preserve_appointments and encoding != self.safe_baseline.encoding:
            raise EncodingError("Original appointment replay requires the safe baseline encoding")
        return self._decode(encoding, preserve_appointments)

    def _decode(self, encoding, preserve_appointments):
        wall, cpu = perf_counter(), process_time()
        repairs = []
        while True:
            try:
                result = self._attempt(encoding, preserve_appointments)
                failure = None
                break
            except _Infeasible as exc:
                failure = exc.conflict
                if failure.request_id in self.fixed_choices:
                    empty = frozen({})
                    result = ((), empty, empty, empty, empty, empty, None, None, None)
                    break
                repairs.append(failure)
                encoding = encoding.without(failure.request_id)
        tasks, cars, nodes, flights, trips, ends, f1, f2, score = result
        return DecodedSchedule(self.batch_key, self.snapshot.time, self.snapshot.version, preserve_appointments, encoding,
                               tasks, cars, nodes, flights, trips, ends, tuple(repairs), len(repairs)+1,
                               failure, f1, f2, score, perf_counter()-wall, process_time()-cpu)

    def _attempt(self, enc, preserve):
        snap = self.snapshot
        plans, chains, sequence = self._resolve(enc)
        cars = {vid: _Car(v.motion.edge.target if v.motion else v.node,
                          v.motion.exact_end if v.motion else Decimal(snap.time),
                          set(v.onboard), v.occupancy) for vid, v in snap.vehicles.items()}
        seats = {fid: dict(f.reservations) for fid, f in snap.flights.items()}
        journeys = {}
        for rid, plan in plans.items():
            req = snap.requests[rid]
            start = req.accepted_at if rid in self.fixed_choices else snap.time
            journey = _Journey(req, plan, chains[rid], start)
            journey.advance_prefix_and_walks(snap)
            journeys[rid] = journey

        output = []
        for t in sequence:
            j = journeys[t.request_id]
            if t.kind == "FLIGHT":
                row = self._flight(j, t, seats[t.resource_id])
            else:
                car = cars[t.resource_id]
                row = self._stop(j, t, car, self.appointments.get(t.task_id) if preserve else None)
                car.stops.append(row)
            output.append(row)
            j.index += 1
            j.advance_prefix_and_walks(snap)
        trips = frozen({rid: tuple(j.trips) for rid, j in journeys.items()})
        ends = frozen({rid: rows[-1].dropoff for rid, rows in trips.items()})
        nodes = {}
        for vid, car in cars.items():
            original = snap.vehicles[vid]
            node = car.node
            # A vehicle without a replacement queue continues its P1 road program.
            if not car.stops or preserve:
                if original.remaining_edges:
                    node = original.remaining_edges[-1].target
                for action in original.actions:
                    if action.kind == "DRIVE":
                        node = action.target
                    elif preserve:
                        node = snap.tasks[action.target].node
                if preserve and car.stops and car.stops[-1].task.task_id not in self.appointments:
                    node = car.node
            nodes[vid] = node
        return (tuple(output), frozen({k: tuple(c.stops) for k, c in cars.items()}), frozen(nodes),
                frozen({k: frozen(v) for k, v in seats.items()}), trips, ends,
                *objective(snap.requests, ends, self.beta, self.delta))

    def _stop(self, j, t, car, appointment):
        rid, req, step = t.request_id, j.request, j.step
        try:
            travel = self.road.shortest_time_s(car.node, t.node)
            if t.kind == "PICKUP" and self.road.shortest_time_s(step.origin, step.destination) != step.nominal_s:
                raise EncodingError(f"Nominal car time differs from P1 for {rid}/{step.leg_id}")
        except UnreachableRoad as exc:
            raise _Infeasible(rid, "unreachable_road", exc) from exc
        begin = car.available
        arrival = event_second(exact_add(begin, travel))
        if appointment is not None:
            arrival = max(arrival, appointment.arrival)
        at = arrival
        if t.kind == "PICKUP":
            at = max(at, j.ready, event_second(step.earliest_pickup or 0))
        # On a single vehicle P1 orders same-second dropoffs before pickups.
        # A dropoff encoded after a pickup at this stop needs the next tick.
        elif car.stops and car.stops[-1].task.kind == "PICKUP":
            at = max(at, car.stops[-1].at + 1)
        if appointment is not None:
            if at > appointment.service:
                raise _Infeasible(rid, "missed_appointment", (t.task_id, at, appointment))
            at = appointment.service
        if t.kind == "PICKUP":
            if rid in car.onboard or car.load + req.party_size > self.snapshot.vehicles[t.resource_id].capacity:
                raise _Infeasible(rid, "vehicle_capacity", (t.task_id, car.load, req.party_size))
            j.board(at)
            car.onboard.add(rid)
            car.load += req.party_size
        else:
            if rid not in car.onboard or at < exact_add(j.pickup, step.nominal_s):
                raise _Infeasible(rid, "invalid_dropoff", t.task_id)
            j.finish(at)
            car.onboard.remove(rid)
            car.load -= req.party_size
        car.node, car.available = t.node, Decimal(at)
        return TimedTask(t, begin, arrival, at, car.load)

    def _flight(self, j, t, seats):
        flight = self.snapshot.flights[t.resource_id]
        step, rid = j.step, t.request_id
        if (flight.status != "SCHEDULED" or (flight.origin, flight.destination) != (step.origin, step.destination)
                or (step.route_id is not None and step.route_id != flight.route_id)
                or event_second(exact_add(flight.depart_at, step.nominal_s)) != flight.arrive_at):
            raise _Infeasible(rid, "incompatible_flight", flight.flight_id)
        if j.ready > flight.depart_at:
            raise _Infeasible(rid, "missed_flight", (flight.flight_id, j.ready, flight.depart_at))
        j.board(flight.depart_at)
        if rid not in self.fixed_choices:
            if rid in seats or sum(seats.values()) + j.request.party_size > flight.capacity:
                raise _Infeasible(rid, "flight_capacity", flight.flight_id)
            seats[rid] = j.request.party_size
        j.finish(flight.arrive_at)
        return TimedTask(t, Decimal(flight.depart_at), flight.depart_at, flight.arrive_at, None)

    def initialize(self, rng):
        """§5.3.4 samples uniformly among IDs with remaining tasks at each draw."""
        base = self.safe_baseline
        choices, sequence = dict(base.encoding.choices), list(base.encoding.sequence)
        remaining = {}
        for rid, typ in sorted(self.snapshot.request_types().items()):
            plans = self.options.get(rid)
            if typ == 3 or not plans:
                continue
            plan = plans[0]
            rows = [self.rows.get((rid, plan.plan_id, s.leg_id), ()) for s in plan.legs if isinstance(s, Leg)]
            if all(rows):
                choice = Choice(plan.plan_id, tuple(rng.choice(row) for row in rows))
                choices[rid] = choice
                remaining[rid] = len(pending_formal(task_chain(plan, choice)))
        while remaining:
            rid = rng.choice(tuple(remaining))
            sequence.append(rid)
            remaining[rid] -= 1
            if not remaining[rid]:
                del remaining[rid]
        trial = self.decode(Encoding(choices, sequence))
        current = trial if trial.feasible else base
        return InitialSolutions(base, current, current if current.better_than(base) else base, trial)
