from dataclasses import replace

import pytest

from src.m5 import M5Rebalancer, RebalanceInvariantError
from tests.p5_examples import request, world


def example():
    sim = world(((0, -73.999, 40.751), (1, -73.998, 40.751),
                 (2, -73.982, 40.754), (3, -73.985, 40.752)),
                [(0, 1, 2), (1, 2, 4), (2, 3, 1)], [("a", 0), ("b", 0), ("local", 2)])
    for rid in ("r", "s", "t"):
        request(sim, rid, 2, 3)
    snap = sim.batch_snapshot()
    plan = M5Rebalancer(sim.router).propose(snap, snap.requests, snap.vehicles)
    assert plan.status == "READY" and plan.local_vehicle_ids == ("local",)
    return sim, snap, plan


def test_publishes_two_moves_and_keeps_local_vehicle():
    from src.simulation.rebalance import commit_rebalance_plan
    sim, snap, plan = example()
    assert commit_rebalance_plan(sim, snap, plan) == 2
    after = sim.batch_snapshot()
    assert after.version == snap.version + 1
    assert after.vehicles["local"] == snap.vehicles["local"]
    assert all(after.vehicles[v].motion.edge.target == 1 for v in ("a", "b"))
    sim.advance_to(6)
    assert all(v.node == 2 for v in sim.snapshot().vehicles.values())
    assert all(r.status == "WAITING" for r in sim.snapshot().requests.values())


def test_foreign_plan_with_same_graph_and_version_is_rejected():
    from src.simulation.rebalance import commit_rebalance_plan
    _, _, foreign = example()
    sim, snap, _ = example()
    assert foreign.snapshot_version == snap.version
    with pytest.raises(ValueError, match="snapshot"):
        commit_rebalance_plan(sim, snap, foreign)
    assert sim.snapshot() == snap


@pytest.mark.parametrize("fault", ["last_edge", "unknown", "stale", "local_quota", "double", "arrival"])
def test_invalid_joint_rebalance_is_atomic(fault):
    from src.simulation.rebalance import commit_rebalance_plan
    sim, snap, plan = example()
    first, last = plan.moves
    if fault == "stale":
        plan = replace(plan, snapshot_version=snap.version - 1)
    elif fault == "local_quota":
        plan = replace(plan, local_vehicle_ids=(), remaining_vehicle_ids=("local",))
    else:
        changes = {"last_edge": {"edges": last.edges[:-1]}, "unknown": {"vehicle_id": "missing"},
                   "double": {"vehicle_id": first.vehicle_id}, "arrival": {"arrive_at": 99}}
        plan = replace(plan, moves=(first, replace(last, **changes[fault])))
    history = sim.history
    with pytest.raises((ValueError, RebalanceInvariantError)):
        commit_rebalance_plan(sim, snap, plan)
    assert sim.snapshot() == snap and sim.history == history
    sim.advance_to(7)
    assert all(v.parked_without_program for v in sim.snapshot().vehicles.values())

