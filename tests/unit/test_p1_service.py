from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext

import pytest

from src.m2.passengers import FlightInstance, Leg, Plan, Request, WalkLink, task_id
from src.simulation.service import ServiceState


def request(rid="r", origin="A", destination="Z", party=1, direct=100, **kwargs):
    return Request(rid, 0, origin, destination, party, direct, **kwargs)


def vehicle_leg(lid, start, end, duration=1, **kwargs):
    return Leg(lid, "CAR", start, end, duration, **kwargs)


def execute(state, kind, oid, t, node=None, capacity=4):
    return state.execute(kind, oid, t, lambda vehicle: node, lambda vehicle: capacity)


def test_ground_air_ground_with_walks_seats_and_history():
    state = ServiceState()
    state.add_request(request(party=2))
    state.add_flight(FlightInstance("f", "P", "Q", 12, 20, 3))
    plan = Plan("p", "r", [
        WalkLink("access", "A", "B", 2.1, 1),
        vehicle_leg("c1", "B", "P", 4),
        Leg("fly", "FLIGHT", "P", "Q", 8),
        vehicle_leg("c2", "Q", "Y", 1),
        WalkLink("exit", "Y", "Z", 2, 1),
    ])
    assert isinstance(plan.legs, tuple)
    walk = task_id("r", "access", "WALK_START")
    end = task_id("r", "access", "WALK_END")
    with pytest.raises(ValueError):
        execute(state, "WALK_START", walk, 0)
    assert state.accept("r", plan, (None, "v1", "f", "v2", None), now=3) == ((3, "WALK_START", walk),)
    assert execute(state, "WALK_START", walk, 3) == ((6, "WALK_END", end),)
    with pytest.raises(ValueError):
        execute(state, "WALK_END", end, 5)
    execute(state, "WALK_END", end, 6)
    with pytest.raises(ValueError):
        execute(state, "PICKUP", task_id("r", "c2", "PICKUP"), 6, "Q")
    execute(state, "PICKUP", task_id("r", "c1", "PICKUP"), 8, "B")
    assert state.onboard("v1") == ("r",)
    assert state.occupancy("v1") == 2
    assert state.requests["r"].first_pickup_at == 8
    with pytest.raises(ValueError):
        execute(state, "DROPOFF", task_id("r", "c1", "DROPOFF"), 9, "P")
    execute(state, "DROPOFF", task_id("r", "c1", "DROPOFF"), 12, "P")
    execute(state, "DEPART", "f", 12)
    assert state.occupancy("v1") == 0
    assert state.requests["r"].status == "ON_FLIGHT"
    assert state.flights["f"].remaining_seats == 1
    with pytest.raises(ValueError):
        state.accept("r", plan, (None, "v1", "f", "other", None), now=12)
    execute(state, "LAND", "f", 20)
    execute(state, "PICKUP", task_id("r", "c2", "PICKUP"), 20, "Q")
    last_walk = task_id("r", "exit", "WALK_START")
    assert execute(state, "DROPOFF", task_id("r", "c2", "DROPOFF"), 21, "Y") == (
        (21, "WALK_START", last_walk),
    )
    execute(state, "WALK_START", last_walk, 21)
    execute(state, "WALK_END", task_id("r", "exit", "WALK_END"), 23)
    result = state.requests["r"]
    assert (result.node, result.completed_at, result.status) == ("Z", 23, "COMPLETED")
    assert len(result.history) == len(set(result.history)) == 9
    assert result.bindings == (None, "v1", "f", "v2", None)
    with pytest.raises(FrozenInstanceError):
        result.status = "WAITING"
    with pytest.raises(TypeError):
        state.requests["r"] = result
    with pytest.raises(ValueError):
        execute(state, "WALK_END", task_id("r", "exit", "WALK_END"), 23)


def test_joint_reservations_and_missed_locked_flight_are_atomic():
    state = ServiceState()
    state.add_flight(FlightInstance("f", "P", "Q", 5, 10, 3))
    for rid, origin, party in [("a", "P", 2), ("b", "O", 1), ("c", "P", 1)]:
        state.add_request(request(rid, origin, "Q", party))
        legs = (Leg("air", "FLIGHT", "P", "Q", 5),)
        bindings = ("f",)
        if origin == "O":
            legs = (vehicle_leg("access", "O", "P"),) + legs
            bindings = ("car",) + bindings
        if rid == "c":
            with pytest.raises(ValueError):
                state.accept(rid, Plan("p", rid, legs), bindings, now=0)
            assert state.requests["c"].status == "WAITING"
        else:
            state.accept(rid, Plan("p", rid, legs), bindings, now=0)
    assert state.flights["f"].reserved_seats == 3
    with pytest.raises(ValueError):
        execute(state, "DEPART", "f", 5)
    assert state.requests["a"].status == "ACCEPTED"
    assert state.tasks[task_id("a", "air", "FLIGHT")].status == "PENDING"
    execute(state, "PICKUP", task_id("b", "access", "PICKUP"), 3, "O")
    execute(state, "DROPOFF", task_id("b", "access", "DROPOFF"), 5, "P")
    with pytest.raises(ValueError):
        execute(state, "DEPART", "f", 6)
    execute(state, "DEPART", "f", 5)
    execute(state, "LAND", "f", 10)
    assert [(state.requests[i].completed_at, state.requests[i].node) for i in "ab"] == [(10, "Q"), (10, "Q")]


def test_multicar_transfer_full_vehicle_and_readiness():
    state = ServiceState()
    for r in "ab":
        state.add_request(request(r, party=3))
        state.accept(r, Plan("p", r, (
            vehicle_leg("first", "A", "X"), vehicle_leg("next", "X", "Z"),
        )), ("v", "w"), now=0)
    execute(state, "PICKUP", task_id("a", "first", "PICKUP"), 1, "A")
    with pytest.raises(ValueError):
        execute(state, "PICKUP", task_id("b", "first", "PICKUP"), 2, "A")
    with pytest.raises(ValueError):
        execute(state, "PICKUP", task_id("a", "next", "PICKUP"), 2, "X")
    assert state.onboard("v") == ("a",)
    execute(state, "DROPOFF", task_id("a", "first", "DROPOFF"), 2, "X")
    execute(state, "PICKUP", task_id("a", "next", "PICKUP"), 2, "X")
    execute(state, "DROPOFF", task_id("a", "next", "DROPOFF"), 3, "Z")
    assert state.requests["a"].completed_at == 3


def test_inclusive_first_pickup_and_final_floor_for_fractional_deadline():
    state = ServiceState()
    state.add_request(request(direct=1.2))
    plan = Plan("p", "r", (vehicle_leg("c", "A", "Z"),))
    state.accept("r", plan, ("v",), now=420)
    with pytest.raises(ValueError):
        execute(state, "PICKUP", task_id("r", "c", "PICKUP"), 421, "A")
    execute(state, "PICKUP", task_id("r", "c", "PICKUP"), 420, "A")
    with pytest.raises(ValueError):
        execute(state, "DROPOFF", task_id("r", "c", "DROPOFF"), 422, "Z")
    execute(state, "DROPOFF", task_id("r", "c", "DROPOFF"), 421, "Z")
    assert state.requests["r"].completed_at == 421


def test_expiry_and_prearrival_and_forged_state():
    state = ServiceState()
    req = Request("r", 10, "A", "Z", 1, 2)
    plan = Plan("p", "r", (vehicle_leg("c", "A", "Z"),))
    with pytest.raises(ValueError):
        state.accept("r", plan, ("v",), now=0)
    with pytest.raises(ValueError):
        state.add_request(replace(req, status="COMPLETED"))
    with pytest.raises(ValueError):
        state.add_flight(replace(FlightInstance("f", "A", "Z", 20, 22), status="LANDED"))
    state.add_request(req)
    with pytest.raises(ValueError):
        state.accept("r", plan, ("v",), now=9)
    with pytest.raises(ValueError):
        execute(state, "EXPIRE", "r", 430)
    execute(state, "EXPIRE", "r", 431)
    assert state.requests["r"].status == "EXPIRED_UNSERVED"
    with pytest.raises(ValueError):
        state.accept("r", plan, ("v",), now=431)


def test_flight_unreachable_on_foot_and_local_windows_reject_without_locks():
    state = ServiceState()
    state.add_request(request())
    state.add_flight(FlightInstance("f", "X", "Z", 10, 12))
    plan = Plan("p", "r", (WalkLink("w", "A", "X", 12, 1), Leg("a", "FLIGHT", "X", "Z", 2)))
    with pytest.raises(ValueError):
        state.accept("r", plan, (None, "f"), now=0)
    assert state.flights["f"].reserved_seats == 0
    assert not state.tasks
    leg = vehicle_leg("c", "A", "Z", 1, earliest_pickup=4, latest_pickup=6, latest_dropoff=8)
    state.accept("r", Plan("p2", "r", (leg,)), ("v",), now=0)
    for kind, time, node in [("PICKUP", 3, "A"), ("PICKUP", 7, "A"), ("PICKUP", 5, "X")]:
        with pytest.raises(ValueError):
            execute(state, kind, task_id("r", "c", kind), time, node)
    execute(state, "PICKUP", task_id("r", "c", "PICKUP"), 5, "A")
    with pytest.raises(ValueError):
        execute(state, "DROPOFF", task_id("r", "c", "DROPOFF"), 9, "Z")
    execute(state, "DROPOFF", task_id("r", "c", "DROPOFF"), 8, "Z")


def test_fixed_flight_matches_nominal_ceiling_and_selected_directional_route():
    state = ServiceState()
    state.add_request(request())
    state.add_flight(FlightInstance("f", "A", "Z", 5, 8, route_id="az"))
    for duration, route in [(2, "az"), (3.1, "az"), (3, "za")]:
        planned = Plan("p", "r", (Leg("air", "FLIGHT", "A", "Z", duration, route_id=route),))
        with pytest.raises(ValueError):
            state.accept("r", planned, ("f",), now=0)
        assert state.flights["f"].reserved_seats == 0 and not state.tasks
    planned = Plan("p", "r", (Leg("air", "FLIGHT", "A", "Z", 2.1, route_id="az"),))
    state.accept("r", planned, ("f",), now=0)
    assert state.flights["f"].reserved_seats == 1


def test_walk_geometry_and_plan_continuity():
    with pytest.raises(ValueError):
        WalkLink("w", "A", "B", 0, 1)
    with pytest.raises(ValueError):
        WalkLink("w", "A", "B", 500, 1)
    with pytest.raises(ValueError):
        Plan("p", "r", (vehicle_leg("c1", "A", "B"), vehicle_leg("c2", "X", "Z")))
    assert task_id("a:b", "c", "PICKUP") != task_id("a", "b:c", "PICKUP")


def test_watchdogs_detect_stalled_accepted_passengers_and_ignore_terminal_states():
    state = ServiceState()
    state.add_request(request(direct=1.2))
    plan = Plan("p", "r", (vehicle_leg("c", "A", "Z"),))
    state.accept("r", plan, ("v",), now=0)
    assert state.accept("r", plan, ("v",), now=10) == ()
    with pytest.raises(ValueError):
        execute(state, "EXPIRE", "r", 421)
    execute(state, "PICKUP", task_id("r", "c", "PICKUP"), 420, "A")
    assert execute(state, "EXPIRE", "r", 421) == ()
    with pytest.raises(ValueError):
        execute(state, "EXPIRE_FINAL", "r", 422)
    execute(state, "DROPOFF", task_id("r", "c", "DROPOFF"), 421, "Z")
    assert execute(state, "EXPIRE_FINAL", "r", 422) == ()


def test_domain_enums_cohort_and_nested_collections_cannot_be_forged():
    from src.m2.passengers import FlightStatus, RequestStatus, TaskKind, TaskStatus

    coords = [1, 2]
    req = request(cohort="EVALUATION", original_origin=coords)
    coords[0] = 5
    assert request(cohort="EVAL").cohort == req.cohort == "EVALUATION"
    assert req.original_origin == (1.0, 2.0)
    assert req.status is RequestStatus.WAITING
    with pytest.raises(ValueError):
        replace(req, status="INVALID")
    f = FlightInstance("f", "A", "Z", 1, 3)
    assert f.status is FlightStatus.SCHEDULED
    with pytest.raises(ValueError):
        replace(f, status="INVALID")
    records = [["r", 1]]
    locked = replace(f, reservations=records)
    records[0][0] = "other"
    assert locked.reservations == (("r", 1),)
    state = ServiceState()
    state.add_request(req)
    resources = ["v"]
    plan = Plan("p", "r", (vehicle_leg("c", "A", "Z"),))
    state.accept("r", plan, resources, now=0)
    resources[0] = "other"
    task = state.tasks[task_id("r", "c", "PICKUP")]
    assert task.kind is TaskKind.PICKUP and task.status is TaskStatus.PENDING
    assert state.requests["r"].bindings == ("v",)


def test_small_decimal_context_preserves_absolute_deadline_and_walk_ceiling():
    state = ServiceState()
    with localcontext() as ctx:
        ctx.prec = 3
        req = Request("r", 1000, "A", "Z", 1, "1.000000000000000000001")
        assert req.final_deadline == Decimal("1421.000000000000000000001")
        walk = WalkLink("w", "A", "P", "3.00000000000000000000001", 3)
        plan = Plan("p", "r", (walk, vehicle_leg("c", "P", "Z")))
        assert plan.nominal_s > Decimal("2")
        state.add_request(req)
        state.accept("r", plan, (None, "v"), now=1000)
        events = execute(state, "WALK_START", task_id("r", "w", "WALK_START"), 1000)
        assert events == ((1002, "WALK_END", task_id("r", "w", "WALK_END")),)
