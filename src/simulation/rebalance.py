"""Publish an entire D010 post-passenger transport solution on the P1 event engine."""
from collections import Counter
from decimal import Decimal

from src.common.timebase import event_second, exact_add
from src.m5 import RebalanceInvariantError
from src.simulation.events import committed_car_ids


def _require(condition, message):
    if not condition:
        raise RebalanceInvariantError(message, None)


def commit_rebalance_plan(sim, snapshot, plan):
    """The single-writer simulator switches versions once, after checking every move."""
    if (plan.world_id != snapshot.world_id or plan.snapshot_version != snapshot.version or plan.batch_time != snapshot.time
            or plan.grid.graph_version != sim.router.version):
        raise ValueError("Stale M5 snapshot, time or road graph")
    if plan.status in {"SKIPPED", "SOLVER_FAILED", "UNEXECUTABLE"}:
        _require(not plan.moves, "Unpublished M5 plan contains vehicle moves")
        return 0
    _require(plan.status == "READY" and plan.transport is not None
             and plan.transport.status == "OPTIMAL" and plan.route_failure is None,
             "READY requires one executable optimal transport solution")
    staged = sim.stage(snapshot)
    grid = plan.grid
    locked = committed_car_ids(snapshot.tasks.values())
    idle = {vid: grid.node_cells[car.node] for vid, car in snapshot.vehicles.items()
            if car.parked_without_program and vid not in locked}
    demand = Counter(grid.node_cells[r.origin] for r in snapshot.requests.values()
                     if r.status == "WAITING" and r.submitted_at <= snapshot.time
                     <= min(r.pickup_deadline, r.final_deadline))
    _require(dict(Counter(idle.values())) == plan.supply and dict(demand) == plan.demand,
             "M5 supply or unmet demand differs from committed state")
    ids = [move.vehicle_id for move in plan.moves] + list(plan.local_vehicle_ids) + list(plan.remaining_vehicle_ids)
    _require(len(ids) == len(set(ids)) and set(ids) == set(idle),
             "Moving, retained and unallocated vehicles must partition idle supply")
    flow = Counter((idle[v], idle[v]) for v in plan.local_vehicle_ids)
    for move in plan.moves:
        vid = move.vehicle_id
        _require(move.origin == snapshot.vehicles[vid].node and idle[vid] == move.source
                 and move.target != move.source and move.destination in grid.entries.get(move.target, ()),
                 f"Invalid grid endpoints for vehicle {vid}")
        edges = sim.router.shortest_path_edges(move.origin, move.destination)
        duration = sim.router.shortest_time_s(move.origin, move.destination)
        arrival = exact_add(snapshot.time, duration)
        _require(bool(edges) and edges == move.edges and move.travel_time_s == duration
                 and move.depart_at == snapshot.time and move.arrive_at == event_second(arrival)
                 and move.exact_arrival_s == arrival
                 and move.distance_m == sum((edge.length_m for edge in edges), Decimal(0)),
                 f"Invalid directed road program or timing for {vid}")
        flow[move.source, move.target] += 1
    covered = Counter()
    for (_, target), count in flow.items():
        covered[target] += count
    _require(dict(flow) == plan.transport.flow and dict(covered) == plan.covered_demand
             and all(covered[cell] <= demand[cell] for cell in covered)
             and dict(demand - covered) == plan.uncovered_demand
             and sum(covered.values()) == min(len(idle), sum(demand.values())),
             "Grid flow differs from movements, local retention or demand")
    if plan.moves:
        for move in plan.moves:
            staged.install_rebalance_edges(move.vehicle_id, move.destination, move.edges)
        sim.publish(snapshot, staged)
    return len(plan.moves)

