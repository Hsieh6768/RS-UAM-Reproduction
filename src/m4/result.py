"""Frozen candidate output, scoped to one batch and objective configuration."""
from dataclasses import dataclass, field
from decimal import Decimal
from math import fsum
from typing import Mapping, Optional

from src.common.timebase import objective_minutes
from src.m2.passengers import Node, Task
from .encoding import Encoding


@dataclass(frozen=True)
class Conflict:
    request_id: str
    reason: str
    detail: str


@dataclass(frozen=True)
class TripTime:
    leg_id: str
    ready: int
    pickup: int
    dropoff: int


@dataclass(frozen=True)
class TimedTask:
    task: Task
    begin: Decimal  # earliest physical vehicle departure, or flight departure
    arrival: int    # road arrival at stop before waiting, or flight departure
    at: int         # service event, or flight landing
    onboard_after: Optional[int]


@dataclass(frozen=True)
class DecodedSchedule:
    batch_key: object = field(repr=False)
    time: int
    version: int
    preserve_appointments: bool
    encoding: Encoding
    tasks: tuple[TimedTask, ...]
    vehicles: Mapping[str, tuple[TimedTask, ...]]
    final_nodes: Mapping[str, Node]
    flight_seats: Mapping[str, Mapping[str, int]]
    trips: Mapping[str, tuple[TripTime, ...]]
    completion: Mapping[str, int]
    repairs: tuple[Conflict, ...]
    attempts: int
    failure: Optional[Conflict]
    f1: Optional[float]
    f2: Optional[float]
    v: Optional[float]
    wall_s: float = field(compare=False)
    cpu_s: float = field(compare=False)

    @property
    def feasible(self):
        return self.failure is None

    def better_than(self, other):
        if self.batch_key is not other.batch_key:
            raise ValueError("Scores require the same batch evaluation context")
        if not self.feasible or not other.feasible:
            raise ValueError("Only complete feasible schedules have comparable scores")
        return self.v > other.v


def objective(requests, completion, beta, delta):
    f1 = delta * len(completion)
    f2 = fsum(objective_minutes(at - requests[rid].submitted_at) for rid, at in completion.items())
    return f1, f2, beta * f1 - (1 - beta) * f2


@dataclass(frozen=True)
class InitialSolutions:
    safe_baseline: DecodedSchedule
    current_solution: DecodedSchedule
    best_solution: DecodedSchedule
    random_candidate: DecodedSchedule
