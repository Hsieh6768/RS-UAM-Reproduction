"""Bounded P4-B smoke. Three artificial solves of at most 61 candidates each."""
from random import Random

from src.m3 import M3Searcher
from src.m4.budget import aggregate_timings
from src.m4.halns import M4Config, M4HALNS
from tests.p4_examples import leg, release, setup


def main():
    roads = [(0, 1, "3.3"), (1, 0, "2.2"), (1, 2, "3.3"), (2, 1, "2.2")]
    sim = setup(roads, [(f"V{i}", i % 3, 3) for i in range(4)])
    plans = {}
    for i in range(6):
        rid = f"R{i}"
        origin = i % 2
        plan = release(sim, rid, [leg("c", origin, origin + 1, "3.3")], size=2)
        plans[rid] = (plan,)
    snapshot = sim.batch_snapshot()
    sparse = M3Searcher(sim.router).search(snapshot, plans)
    timings = []
    for _ in range(3):
        out = M4HALNS(sim.router, M4Config(inner_loops=5, wall_budget_seconds=2)).solve(
            snapshot, plans, sparse, rng=Random(20261120))
        print(f"v={out.best_solution.v:.6f}, candidates={out.stats.candidates_completed}, "
              f"repairs={sum(out.stats.repair_reasons.values())}, "
              f"component_sizes={dict(sorted(out.stats.component_size_counts.items()))}, "
              f"component_evaluations={out.stats.evaluated_components}, "
              f"reused={out.stats.reused_components}, wall={out.timing.wall_s:.6f}, cpu={out.timing.cpu_s:.6f}")
        timings.append(out.timing)
    print(aggregate_timings(timings))


if __name__ == "__main__":
    main()
