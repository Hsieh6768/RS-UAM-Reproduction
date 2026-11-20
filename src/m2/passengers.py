"""P1 immutable service objects (paper §3.2–3.3, D001/D002/D004/D011)."""

from dataclasses import dataclass
from decimal import Context, Decimal, ROUND_CEILING, localcontext
from enum import Enum
from functools import reduce
from typing import Optional, Tuple, Union

from src.common.timebase import exact_add, nonnegative_decimal, require_tick, seconds


Node = Union[int, str]


class RequestStatus(str, Enum):
    WAITING = "WAITING"
    ACCEPTED = "ACCEPTED"
    ON_VEHICLE = "ON_VEHICLE"
    ON_FLIGHT = "ON_FLIGHT"
    WALKING = "WALKING"
    COMPLETED = "COMPLETED"
    EXPIRED_UNSERVED = "EXPIRED_UNSERVED"


class FlightStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    AIRBORNE = "AIRBORNE"
    LANDED = "LANDED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"


class TaskKind(str, Enum):
    PICKUP = "PICKUP"
    DROPOFF = "DROPOFF"
    FLIGHT = "FLIGHT"
    WALK_START = "WALK_START"
    WALK_END = "WALK_END"


class LegMode(str, Enum):
    CAR = "CAR"
    FLIGHT = "FLIGHT"


class Cohort(str, Enum):
    WARMUP = "WARMUP"
    EVALUATION = "EVALUATION"
    TAIL = "TAIL"


def identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Expected a nonempty ID, got {value!r}")
    return value


def check_nodes(origin, destination):
    for node in (origin, destination):
        if type(node) is not int and not (isinstance(node, str) and node.strip()):
            raise ValueError(f"Invalid location {node!r}")


def task_id(request_id: str, leg_id: str, kind: str) -> str:
    """Length-prefixed IDs remain unique even when an input ID contains ':'."""
    return f"{len(request_id)}:{request_id}:{len(leg_id)}:{leg_id}:{TaskKind(kind).value}"


@dataclass(frozen=True)
class Leg:
    leg_id: str
    mode: LegMode
    origin: Node
    destination: Node
    nominal_s: Decimal
    earliest_pickup: Optional[Decimal] = None
    latest_pickup: Optional[Decimal] = None
    latest_dropoff: Optional[Decimal] = None
    route_id: Optional[str] = None

    def __post_init__(self):
        identifier(self.leg_id)
        check_nodes(self.origin, self.destination)
        object.__setattr__(self, "mode", LegMode(self.mode))
        for name in ("nominal_s", "earliest_pickup", "latest_pickup", "latest_dropoff"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, seconds(value))
        if self.nominal_s is None or (self.origin != self.destination and self.nominal_s == 0):
            raise ValueError("Travel between distinct locations requires positive nominal time")
        # D002: LP bounds pickup and LD bounds dropoff, independently.
        if self.earliest_pickup is not None and any(
            self.earliest_pickup > limit
            for limit in (self.latest_pickup, self.latest_dropoff) if limit is not None
        ):
            raise ValueError("Inconsistent leg time window")
        if self.route_id is not None:
            identifier(self.route_id)
            if self.mode != "FLIGHT":
                raise ValueError("Fixed air route is only applicable to a flight leg")


@dataclass(frozen=True)
class WalkLink:
    leg_id: str
    origin: Node
    destination: Node
    distance_m: Decimal
    speed_mps: Decimal

    def __post_init__(self):
        identifier(self.leg_id)
        check_nodes(self.origin, self.destination)
        object.__setattr__(self, "distance_m", nonnegative_decimal(self.distance_m, "metres"))
        object.__setattr__(self, "speed_mps", nonnegative_decimal(self.speed_mps, "metres per second"))
        if self.speed_mps == 0 or self.distance_m >= 500:
            raise ValueError("Auxiliary walks require positive speed and distance below 500 metres")
        if self.distance_m == 0 and self.origin != self.destination:
            raise ValueError("Zero-time transfers must be co-located")

    @property
    def nominal_s(self):
        # Division may recur. Upward rounding with sufficient integer digits
        # preserves its exact integer ceiling for a walk starting on an event tick.
        integer_digits = max(1, self.distance_m.adjusted() - self.speed_mps.adjusted() + 2)
        input_digits = len(self.distance_m.as_tuple().digits) + len(self.speed_mps.as_tuple().digits)
        with localcontext(Context(prec=integer_digits + max(input_digits, 34), rounding=ROUND_CEILING)):
            return self.distance_m / self.speed_mps


@dataclass(frozen=True)
class Plan:
    plan_id: str
    request_id: str
    legs: Tuple[Union[Leg, WalkLink], ...]

    def __post_init__(self):
        identifier(self.plan_id)
        identifier(self.request_id)
        object.__setattr__(self, "legs", tuple(self.legs))
        if not all(isinstance(step, (Leg, WalkLink)) for step in self.legs):
            raise ValueError("Unknown plan step")
        if not any(isinstance(step, Leg) for step in self.legs):
            raise ValueError("A service plan requires a formal transport leg")
        if len({step.leg_id for step in self.legs}) != len(self.legs):
            raise ValueError("Duplicate leg ID in service plan")
        if any(a.destination != b.origin for a, b in zip(self.legs, self.legs[1:])):
            raise ValueError("Disconnected service plan")

    @property
    def nominal_s(self):
        return reduce(exact_add, (step.nominal_s for step in self.legs), Decimal(0))

    @property
    def formal_leg_count(self):
        return sum(isinstance(step, Leg) for step in self.legs)

    @property
    def transfer_count(self):
        return self.formal_leg_count - 1

    @property
    def service_mode(self):
        if any(isinstance(step, Leg) and step.mode == LegMode.FLIGHT for step in self.legs):
            return "RS-UAM"
        return "SHRS" if self.formal_leg_count == 1 else "MHRS"

    def step_neighbors(self, leg_id):
        """Immediate predecessor/successor IDs in the chain, including walks."""
        for i, step in enumerate(self.legs):
            if step.leg_id == leg_id:
                previous = self.legs[i - 1].leg_id if i else None
                following = self.legs[i + 1].leg_id if i + 1 < len(self.legs) else None
                return previous, following
        raise KeyError(leg_id)


@dataclass(frozen=True)
class Task:
    task_id: str
    request_id: str
    leg_id: str
    kind: TaskKind
    resource_id: Optional[str]
    node: Node
    predecessor: Optional[str]
    status: TaskStatus = TaskStatus.PENDING
    started_at: Optional[int] = None
    completed_at: Optional[int] = None

    def __post_init__(self):
        identifier(self.task_id)
        identifier(self.request_id)
        identifier(self.leg_id)
        check_nodes(self.node, self.node)
        for value in (self.resource_id, self.predecessor):
            if value is not None:
                identifier(value)
        object.__setattr__(self, "kind", TaskKind(self.kind))
        object.__setattr__(self, "status", TaskStatus(self.status))
        for tick in (self.started_at, self.completed_at):
            if tick is not None:
                require_tick(tick)


@dataclass(frozen=True)
class Request:
    request_id: str
    submitted_at: int
    origin: Node
    destination: Node
    party_size: int
    direct_road_time_s: Decimal
    pickup_tolerance_s: int = 420
    delay_tolerance_s: int = 420
    cohort: Cohort = Cohort.EVALUATION
    original_origin: Optional[Tuple[float, float]] = None
    original_destination: Optional[Tuple[float, float]] = None
    status: RequestStatus = RequestStatus.WAITING
    plan: Optional[Plan] = None
    bindings: Tuple[Optional[str], ...] = ()
    step_index: int = 0
    node: Optional[Node] = None
    ready_at: Optional[int] = None
    accepted_at: Optional[int] = None
    first_pickup_at: Optional[int] = None
    completed_at: Optional[int] = None
    history: Tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.request_id)
        check_nodes(self.origin, self.destination)
        require_tick(self.submitted_at)
        require_tick(self.pickup_tolerance_s)
        require_tick(self.delay_tolerance_s)
        object.__setattr__(self, "direct_road_time_s", seconds(self.direct_road_time_s))
        if type(self.party_size) is not int or not 1 <= self.party_size <= 4:
            raise ValueError("Requests require one to four passengers")
        object.__setattr__(self, "status", RequestStatus(self.status))
        object.__setattr__(self, "cohort", Cohort("EVALUATION" if self.cohort == "EVAL" else self.cohort))
        object.__setattr__(self, "bindings", tuple(identifier(r) if r is not None else None for r in self.bindings))
        object.__setattr__(self, "history", tuple(identifier(t) for t in self.history))
        if self.plan is not None and not isinstance(self.plan, Plan):
            raise ValueError("Unknown plan type")
        if self.node is not None:
            check_nodes(self.node, self.node)
        require_tick(self.step_index)
        for tick in (self.ready_at, self.accepted_at, self.first_pickup_at, self.completed_at):
            if tick is not None:
                require_tick(tick)
        for field in ("original_origin", "original_destination"):
            point = getattr(self, field)
            if point is not None:
                x, y = point
                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    raise ValueError("Invalid longitude / latitude")
                object.__setattr__(self, field, (float(x), float(y)))

    @property
    def pickup_deadline(self):
        return self.submitted_at + self.pickup_tolerance_s

    @property
    def final_deadline(self):
        return exact_add(self.submitted_at + self.delay_tolerance_s, self.direct_road_time_s)


@dataclass(frozen=True)
class FlightInstance:
    flight_id: str
    origin: Node
    destination: Node
    depart_at: int
    arrive_at: int
    capacity: int = 4
    route_id: Optional[str] = None
    reservations: Tuple[Tuple[str, int], ...] = ()
    status: FlightStatus = FlightStatus.SCHEDULED

    def __post_init__(self):
        identifier(self.flight_id)
        check_nodes(self.origin, self.destination)
        require_tick(self.depart_at)
        require_tick(self.arrive_at)
        if self.route_id is not None:
            identifier(self.route_id)
        if self.arrive_at <= self.depart_at:
            raise ValueError("Flight must land after departure")
        if type(self.capacity) is not int or self.capacity <= 0:
            raise ValueError("Flight capacity must be a positive integer")
        object.__setattr__(self, "status", FlightStatus(self.status))
        records = tuple((identifier(rid), count) for rid, count in self.reservations)
        if any(type(count) is not int or count <= 0 for _, count in records):
            raise ValueError("Reserved seat counts must be positive integers")
        if len({rid for rid, _ in records}) != len(records) or sum(n for _, n in records) > self.capacity:
            raise ValueError("Duplicate or excess flight reservation")
        object.__setattr__(self, "reservations", records)

    @property
    def reserved_seats(self):
        return sum(size for _, size in self.reservations)

    @property
    def remaining_seats(self):
        return self.capacity - self.reserved_seats
