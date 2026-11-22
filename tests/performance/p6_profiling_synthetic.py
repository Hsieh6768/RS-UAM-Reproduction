"""Bounded P6 profile comparison: two three-node worlds, 7 paired solves each."""
from random import Random
from statistics import median
from time import perf_counter_ns

from src.evaluation.profiling import BatchProfile, ProfileConfig
from src.m3 import M3Searcher
from src.m4.halns import M4Config, M4HALNS
from tests.p4_examples import leg, release, setup


def fixture(latest_pickup):
    sim = setup([(0, 1, 11), (1, 2, 1), (2, 1, 1)], [("v", 0, 8)])
    plans = {}
    for i in range(8):
        rid = f"R{i}"
        plans[rid] = (release(sim, rid, [leg("c", 1, 2, 1, lp=latest_pickup)]),)
    snap = sim.batch_snapshot()
    return sim, snap, plans, M3Searcher(sim.router).search(snap, plans)


def main():
    for label, lp in (("open_window", 420), ("pickup_conflict", 10)):
        sim, snap, plans, sparse = fixture(lp)
        solver = M4HALNS(sim.router, M4Config(outer_loops_per_request=1, inner_loops=4, validate_final=True))
        times = {False: [], True: []}
        observed = {}
        reference = None
        for repeat in range(7):
            for enabled in ([False, True] if repeat % 2 == 0 else [True, False]):
                observer = BatchProfile(ProfileConfig(enabled=True), 1) if enabled else None
                if observer is not None:
                    observer.select_requests(snap, plans, sparse)
                start = perf_counter_ns()
                result = solver.solve(snap, plans, sparse, rng=Random(101), profile=observer,
                                      wall_clock=lambda: 0., cpu_clock=lambda: 0.)
                times[enabled].append((perf_counter_ns()-start) / 1e6)
                work = (result.stats, result.best_solution.encoding, result.state)
                if reference is None:
                    reference = work
                assert work == reference and result.stats.candidates_completed == 33
                assert bool(result.stats.repair_reasons.get("pickup_window")) == (lp == 10)
                if observer:
                    for key, row in observer.stages.items():
                        observed.setdefault(key, []).append(row["exclusive_ns"] / 1e6)
        print(label, "repairs", dict(result.stats.repair_reasons), "median_solve_ms(off,on)",
              *(round(median(times[e]), 3) for e in (False, True)))
        print("median_observed_exclusive_ms", {key: round(median(v), 4) for key, v in observed.items()})


if __name__ == "__main__":
    main()
