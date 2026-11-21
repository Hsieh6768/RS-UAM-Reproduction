"""Independent WCC evaluation over the same frozen batch and physics as P4-A."""
from dataclasses import dataclass
from time import perf_counter, process_time
from typing import Optional

from .baseline import record_failure
from .decoder import _Infeasible, _unchecked
from .encoding import frozen
from .result import Conflict, DecodedSchedule, objective
from .task_graph import TaskGraph
from .validation import ValidationError


@dataclass(frozen=True)
class PartitionStats:
    component_sizes: tuple[int, ...] = ()
    evaluated_components: int = 0
    reused_components: int = 0
    repair_order: tuple[Conflict, ...] = ()


@dataclass(frozen=True)
class _LocalFailure:
    conflict: Conflict
    task_id: Optional[str]


class PartitionedDecoder:
    """Scope reusable components to one FullDecoder, snapshot and scoring policy."""

    def __init__(self, full_decoder):
        self.full = full_decoder
        self._previous = {}
        self._defaults = {}
        self.last_stats = PartitionStats()

    def _local(self, comp, plans, chains, preserve, check):
        vehicle_ids = dict.fromkeys(t.resource_id for t in comp.tasks if t.kind != "FLIGHT")
        flight_ids = dict.fromkeys(t.resource_id for t in comp.tasks if t.kind == "FLIGHT")
        try:
            return self.full._physical({r: plans[r] for r in comp.request_ids},
                                       {r: chains[r] for r in comp.request_ids},
                                       comp.tasks, vehicle_ids, flight_ids, preserve, check)
        except _Infeasible as exc:
            return _LocalFailure(exc.conflict, exc.task_id)

    def _combine(self, parts, sequence, encoding, preserve, check):
        full, snap = self.full, self.full.snapshot
        # Unused vehicles continue their snapshot programs. All flights, even ones
        # without pending tasks, keep their existing reservations in the result.
        if preserve not in self._defaults:
            self._defaults[preserve] = full._physical({}, {}, (), snap.vehicles, snap.flights, preserve, check)
        _, empty_cars, idle_nodes, locked_seats, _, _ = self._defaults[preserve]
        cars, nodes, seats = dict(empty_cars), dict(idle_nodes), dict(locked_seats)
        timed, trips, ends = {}, {}, {}
        for rows, car_rows, final_nodes, flight_rows, journeys, completion in parts:
            check()
            for row in rows:
                tid = row.task.task_id
                if tid in timed:
                    raise ValidationError(f"Duplicate partitioned task {tid}")
                timed[tid] = row
            if trips.keys() & journeys.keys() or journeys.keys() != completion.keys():
                raise ValidationError("Component journeys or completions overlap or are missing")
            cars.update(car_rows)
            nodes.update(final_nodes)
            seats.update(flight_rows)
            trips.update(journeys)
            ends.update(completion)
        ordered = []
        for task in sequence:
            check()
            if task.request_id not in encoding.choices:
                continue  # All occurrences of a repaired request were removed.
            row = timed.pop(task.task_id, None)
            if row is None or row.task != task:
                raise ValidationError(f"Missing or altered partitioned task {task.task_id}")
            ordered.append(row)
        if timed or tuple(row.task.request_id for row in ordered) != encoding.sequence:
            raise ValidationError(f"Partitioned tasks do not reconstruct the retained sequence; extras={tuple(timed)}")
        if trips.keys() != encoding.choices.keys():
            raise ValidationError("Partitioned journeys do not reconstruct the retained request choices")
        # The full decoder initializes all journeys in sorted request ID order.
        trips = frozen({r: trips[r] for r in sorted(trips)})
        ends = frozen({r: ends[r] for r in trips})
        return (tuple(ordered), frozen(cars), frozen(nodes), frozen(seats), trips, ends,
                *objective(snap.requests, ends, full.beta, full.delta))

    def decode(self, encoding, *, preserve_appointments=False, checkpoint=None):
        full, check = self.full, checkpoint or _unchecked
        wall, cpu = perf_counter(), process_time()
        full._check_input(encoding, preserve_appointments)
        repairs, components = [], ()
        evaluated = reused = 0
        failure = None
        cache = dict(self._previous)
        try:
            check()
            try:
                plans, chains, sequence = full._resolve(encoding, check)
            except _Infeasible as exc:
                # The sole physical conflict during resolution is a changed commitment.
                failure = exc.conflict
            if failure is None:
                positions = {t.task_id: i for i, t in enumerate(sequence)}
                components = TaskGraph.from_tasks(enumerate(sequence), plans, check).components
            while failure is None:
                parts, conflicts, live = [], [], {}
                for comp in components:
                    check()
                    choices = tuple((r, encoding.choices[r]) for r in comp.request_ids)
                    key = (preserve_appointments, choices, comp.task_ids)
                    if key in cache:
                        part = cache[key]
                        reused += 1
                    else:
                        evaluated += 1
                        part = self._local(comp, plans, chains, preserve_appointments, check)
                    live[key] = part
                    if isinstance(part, _LocalFailure):
                        # P4-A replays all initial prefixes by request ID before
                        # visiting any formal tasks in global occurrence order.
                        rank = ((0, part.conflict.request_id) if part.task_id is None
                                else (1, positions[part.task_id]))
                        conflicts.append((rank, comp, part.conflict))
                    else:
                        parts.append(part)
                cache = live
                if not conflicts:
                    result = self._combine(parts, sequence, encoding, preserve_appointments, check)
                    break
                _, affected, conflict = min(conflicts, key=lambda item: item[0])
                rid = conflict.request_id
                if rid in full.fixed_choices:
                    failure = conflict
                    break
                repairs.append(conflict)
                encoding = encoding.without(rid)
                # Deleting an entire request cannot join previously independent
                # groups. Rebuild only its group; a deleted bridge can split it.
                remaining = ((i, t) for i, t in zip(affected.positions, affected.tasks) if t.request_id != rid)
                children = TaskGraph.from_tasks(remaining, (r for r in affected.request_ids if r != rid),
                                                check).components
                components = tuple(c for c in components if c is not affected) + children
            if failure is not None:
                empty = frozen({})
                result = ((), empty, empty, empty, empty, empty, None, None, None)
            check()
            self._previous = cache
            return DecodedSchedule(full.batch_key, full.snapshot.time, full.snapshot.version, preserve_appointments,
                                   encoding, *result[:6], tuple(repairs), len(repairs) + 1,
                                   failure, *result[6:], perf_counter()-wall, process_time()-cpu)
        except ValidationError as exc:
            exc.failure_path = record_failure(full.failure_dir, full.snapshot, exc)
            raise
        finally:
            self.last_stats = PartitionStats(tuple(len(c.tasks) for c in components), evaluated, reused, tuple(repairs))
