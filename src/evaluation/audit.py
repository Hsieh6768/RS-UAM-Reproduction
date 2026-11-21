"""Independent final accounting against executed P1 events (D004/D011/D012)."""
import csv
import gzip
from collections import Counter, defaultdict
from decimal import Decimal

from src.m2.passengers import Leg, task_id


def _check(condition, message):
    if not condition:
        raise ValueError("Final audit: " + message)


def audit_world(snapshot, event_file, batch_totals, last_batch, expected_eval, initial_nodes=None):
    requests, vehicles, flights, tasks = snapshot.requests, snapshot.vehicles, snapshot.flights, snapshot.tasks
    arrived, executed, departed, landed = set(), set(), set(), set()
    onboard = defaultdict(set)
    occupied = Counter()
    if initial_nodes is not None:
        _check(set(initial_nodes) == set(vehicles), "initial and final fleet IDs differ")
    last_node = {vid: str(node) for vid, node in (initial_nodes or {}).items()}
    flight_bookings = defaultdict(dict)
    mileage = Counter()
    by_vehicle = defaultdict(Counter)
    event_counts = Counter()
    last_time = -1
    with gzip.open(event_file, "rt", encoding="utf-8", newline="") as stream:
        for e in csv.DictReader(stream):
            kind, obj, tick = e["kind"], e["object_id"], int(e["time"])
            _check(last_time <= tick <= snapshot.time, f"event time reversed or in future at {obj}")
            last_time = tick
            event_counts[kind] += 1
            if kind == "ARRIVAL":
                _check(obj in requests and obj not in arrived and requests[obj].submitted_at == tick,
                       f"missing, duplicate or mistimed request {obj}")
                arrived.add(obj)
            elif kind in ("PICKUP", "DROPOFF", "WALK_START", "WALK_END"):
                t = tasks.get(obj)
                _check(t and t.kind == kind and t.completed_at == tick and obj not in executed,
                       f"missing, duplicate or mistimed task {obj}")
                executed.add(obj)
                if kind in ("PICKUP", "DROPOFF"):
                    vid, rid = e["vehicle_id"], t.request_id
                    _check(vid in vehicles and t.resource_id == vid, f"vehicle identity for {obj}")
                    size = requests[rid].party_size
                    _check(vid not in last_node or last_node[vid] == str(t.node),
                           f"vehicle location at {obj}")
                    last_node[vid] = str(t.node)
                    if kind == "PICKUP":
                        _check(rid not in onboard[vid], f"duplicate boarding {rid}")
                        onboard[vid].add(rid)
                        occupied[vid] += size
                    else:
                        _check(rid in onboard[vid], f"missing boarding {rid}")
                        onboard[vid].remove(rid)
                        occupied[vid] -= size
                    _check(0 <= occupied[vid] <= vehicles[vid].capacity, f"vehicle occupancy {vid}")
            elif kind == "EDGE_END":
                vid = e["vehicle_id"]
                _check(vid in vehicles and e["source"] != "" and e["target"] != "",
                       f"unknown physical edge on {vid}")
                _check(vid not in last_node or last_node[vid] == e["source"],
                       f"discontinuous edges for {vid}")
                last_node[vid] = e["target"]
                meters = Decimal(e["length_m"])
                purpose = e["road_purpose"]
                mileage[purpose] += meters
                by_vehicle[vid][purpose] += meters
            elif kind in ("DEPART", "LAND"):
                f = flights.get(obj)
                ids = departed if kind == "DEPART" else landed
                _check(f and obj not in ids and tick == (f.depart_at if kind == "DEPART" else f.arrive_at),
                       f"missing, duplicate or mistimed flight {kind} {obj}")
                _check(kind == "DEPART" or obj in departed, f"landing without departure {obj}")
                ids.add(obj)

    counts = Counter()
    _check(arrived == set(requests), "released requests and ARRIVAL events disagree")
    for rid, r in requests.items():
        accepted = r.accepted_at is not None
        complete = r.status == "COMPLETED"
        _check(accepted == complete, f"accepted/completed mismatch for {rid}")
        _check(complete or r.status == "EXPIRED_UNSERVED", f"nonterminal request {rid}")
        _check(accepted == (r.plan is not None), f"missing or unaccepted plan {rid}")
        counts["accepted"] += accepted
        counts["expired"] += r.status == "EXPIRED_UNSERVED"
        if r.cohort == "EVALUATION":
            counts["evaluation"] += 1
            counts["completed_evaluation"] += complete
        if not accepted:
            _check(not r.history and r.completed_at is None, f"unserved passenger history {rid}")
            continue
        _check(r.plan.request_id == rid and len(r.plan.legs) == len(r.bindings)
               and r.plan.formal_leg_count >= 1, f"incomplete service commitment {rid}")
        _check(r.submitted_at <= r.accepted_at <= r.first_pickup_at <= r.pickup_deadline
               and r.first_pickup_at <= r.completed_at <= r.final_deadline,
               f"passenger deadlines {rid}")
        chain = []
        boardings = []
        for step, resource in zip(r.plan.legs, r.bindings):
            if isinstance(step, Leg) and step.mode == "FLIGHT":
                kinds = ("FLIGHT",)
                _check(resource in flights and rid not in flight_bookings[resource],
                       f"invalid booked flight {resource} for {rid}")
                flight_bookings[resource][rid] = r.party_size
            elif isinstance(step, Leg):
                kinds = ("PICKUP", "DROPOFF")
                _check(resource in vehicles, f"invalid bound vehicle {resource}")
            else:
                kinds = ("WALK_START", "WALK_END")
                _check(resource is None, f"walk bound to resource {resource}")
            for kind in kinds:
                tid = task_id(rid, step.leg_id, kind)
                t = tasks.get(tid)
                _check(t and t.status == "COMPLETED" and t.resource_id == resource
                       and t.predecessor == (chain[-1] if chain else None), f"broken task chain {tid}")
                _check((tasks[chain[-1]].completed_at if chain else r.accepted_at)
                       <= t.started_at <= t.completed_at, f"task causality {tid}")
                if kind == "FLIGHT":
                    f = flights[resource]
                    _check(resource in landed and (t.started_at, t.completed_at) == (f.depart_at, f.arrive_at),
                           f"missing completed passenger flight {tid}")
                    executed.add(tid)
                if kind in ("FLIGHT", "PICKUP"):
                    boardings.append(t.started_at)
                chain.append(tid)
        _check(tuple(chain) == r.history and r.step_index == len(r.plan.legs)
               and tasks[chain[-1]].completed_at == r.completed_at and r.first_pickup_at == boardings[0],
               f"passenger completion history {rid}")
    _check(executed == set(tasks), "task ledger and executed events disagree")
    for vid, v in vehicles.items():
        _check(v.parked_without_program, f"vehicle program did not close for {vid}")
        _check(not onboard[vid] and occupied[vid] == v.occupancy == 0 and not v.onboard,
               f"final occupancy {vid}")
        if vid in last_node:
            node = v.motion.edge.source if v.motion else v.node
            _check(str(node) == last_node[vid], f"final vehicle node {vid}")
    for fid, f in flights.items():
        _check(dict(f.reservations) == flight_bookings[fid] and f.reserved_seats <= f.capacity,
               f"flight reservation mismatch {fid}")
        _check((fid in departed) == (f.depart_at <= snapshot.time)
               and (fid in landed) == (f.arrive_at <= snapshot.time), f"flight event coverage {fid}")
        expected = "LANDED" if fid in landed else "AIRBORNE" if fid in departed else "SCHEDULED"
        _check(f.status == expected, f"flight status {fid}")
    _check(batch_totals["new_req"] == len(requests) and batch_totals["accepted_new"] == counts["accepted"]
           and batch_totals["expired"] == counts["expired"], "batch/request totals disagree")
    if expected_eval is not None:
        _check(expected_eval == counts["evaluation"], "evaluation denominator differs from prepared time window")
    _check(last_batch and last_batch["evaluation_completed"] == last_batch["evaluation_accepted"]
           == counts["completed_evaluation"], "final evaluation counters disagree")
    return {
        "status": "PASS", "request_count": len(requests), "accepted": counts["accepted"],
        "expired": counts["expired"], "evaluation_count": counts["evaluation"],
        "accepted_evaluation": counts["completed_evaluation"], "completed_evaluation": counts["completed_evaluation"],
        "vehicle_count": len(vehicles), "task_count": len(tasks), "flight_count": len(flights),
        "flight_departures": len(departed), "flight_landings": len(landed),
        "flight_seats_reserved": sum(f.reserved_seats for f in flights.values()),
        "event_counts": dict(event_counts), "event_rows": sum(event_counts.values()),
        "road_metres": dict(mileage), "road_metres_by_vehicle": dict(by_vehicle),
    }
