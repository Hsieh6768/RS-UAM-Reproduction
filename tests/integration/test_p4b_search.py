from dataclasses import replace
from itertools import permutations, product
from random import Random

import pytest

from src.common.config import load_config
from src.m2.passengers import FlightInstance, Plan, task_id
from src.m3 import M3Searcher
from src.m4 import BaselineOrderRequired, Choice, Encoding, FullDecoder, PartitionedDecoder, ScheduleValidator
from src.m4.halns import M4Config, M4HALNS
from src.simulation.events import Action, CandidateRoute
from tests.p4_examples import leg, release, setup


def solve(sim, catalog, config=None, *, rng=None, **kwargs):
    snap = sim.batch_snapshot()
    sparse = M3Searcher(sim.router).search(snap, catalog)
    return M4HALNS(sim.router, config or M4Config()).solve(
        snap, catalog, sparse, rng=Random(37) if rng is None else rng, **kwargs)


def test_carry_forward_critical_order_then_remove_only_executed_task(tmp_path):
    sim = setup([(0, 1, 5), (1, 0, 5)], [("v", 0, 4)])
    plans = [release(sim, rid, [leg("c", 0, 1, 5, lp=lp)], size=4)
             for rid, lp in (("A", 420), ("B", 2))]
    for plan in plans:
        sim.accept(plan.request_id, plan, ("v",))
    with pytest.raises(BaselineOrderRequired) as error:
        solve(sim, {}, failure_dir=tmp_path)
    assert error.value.failure_path.exists()
    prior = solve(sim, {}, remaining_sequence="BBAA", failure_dir=tmp_path)
    assert prior.safe_baseline.completion == {"B": 5, "A": 15}
    pick = task_id("B", "c", "PICKUP")
    sim.commit_route(CandidateRoute(sim.batch_snapshot().version, "v", actions=(
        Action(0, "PICKUP", pick), Action(0, "DRIVE", 1))))
    sim.advance_to(2)
    snap, history = sim.batch_snapshot(), sim.history
    later = solve(sim, {}, previous=prior.state, failure_dir=tmp_path)
    assert later.safe_baseline.encoding.sequence == ("B", "A", "A")
    assert later.safe_baseline.vehicles["v"][0].begin == 5
    assert later.best_solution.completion == {"B": 5, "A": 15}
    assert all(row.task.task_id != pick for row in later.best_solution.tasks)
    assert (sim.batch_snapshot(), sim.history) == (snap, history)
    damaged = replace(prior.state, remaining_task_ids=prior.state.remaining_task_ids[:-1])
    with pytest.raises(BaselineOrderRequired) as error:
        solve(sim, {}, previous=damaged, failure_dir=tmp_path)
    assert error.value.failure_path.exists()


def test_replay_inherits_scores_and_rng_and_rescores_on_new_snapshot():
    sim = setup([(0, 1, 10)], [("v", 0, 4)])
    plan = release(sim, "A", [leg("a", 0, 1, 10)])
    catalog = {"A": (plan,)}
    initial = solve(sim, catalog)
    repeated = solve(sim, catalog)
    assert initial.state == repeated.state
    assert initial.stats == repeated.stats
    assert initial.stats.outer_completed == 2 and initial.stats.inner_completed == 50
    assert initial.stats.final_temperature == pytest.approx(70 * .7 ** 50)
    assert initial.stats.operator_draws and len(initial.stats.operator_draws) == 2
    sim.advance_to(20)
    later = solve(sim, catalog, previous=initial.state)
    replay = solve(sim, catalog, previous=initial.state, rng=Random(999))
    assert later.state == replay.state
    assert later.initial_scores == initial.state.scores
    assert later.best_solution.v < initial.best_solution.v
    assert later.best_solution.completion == {"A": 30}


def test_noD_bypasses_partition_entirely_and_validation_runs_once(monkeypatch):
    from src.m4.task_graph import TaskGraph
    def forbidden(*args, **kwargs):
        pytest.fail("noD called partition logic")
    monkeypatch.setattr(TaskGraph, "build", forbidden)
    monkeypatch.setattr(TaskGraph, "from_tasks", forbidden)
    original = ScheduleValidator.validate
    validated = []
    def record(self, result):
        validated.append(result)
        original(self, result)
    monkeypatch.setattr(ScheduleValidator, "validate", record)
    sim = setup([(0, 1, 10)], [("v", 0, 4)])
    plan = release(sim, "A", [leg("a", 0, 1, 10)])
    out = solve(sim, {"A": (plan,)}, M4Config(noD=True, noS=True, noA=True, validate_final=True))
    assert validated == [out.best_solution]
    assert out.state.scores == out.initial_scores
    assert out.stats.inner_completed == 50


def test_budget_after_baseline_and_during_candidate(monkeypatch):
    from src.m4.partitioned_decoder import PartitionedDecoder
    sim = setup([(0, 1, 3)], [("v", 0, 4)])
    plan = release(sim, "A", [leg("c", 0, 1, 3)])
    catalog = {"A": (plan,)}
    calls = iter((0., 12.))
    out = solve(sim, catalog, wall_clock=lambda: next(calls, 12.), cpu_clock=lambda: 0.)
    assert out.timing.timed_out and out.timing.wall_overrun_s == 2.
    assert out.best_solution is out.safe_baseline and out.stats.candidates_started == 0
    now, calls = [0.], []
    original = PartitionedDecoder.decode
    def timed(self, encoding, **kwargs):
        calls.append(encoding)
        if len(calls) == 2:
            now[0] = 12.
        return original(self, encoding, **kwargs)
    monkeypatch.setattr(PartitionedDecoder, "decode", timed)
    out = solve(sim, catalog, wall_clock=lambda: now[0], cpu_clock=lambda: 1.)
    assert out.timing.wall_overrun_s == 2 and out.timing.cpu_s == 0
    assert out.best_solution.completion == {"A": 3} and out.best_solution.feasible
    assert out.stats.candidates_started == 2 and out.stats.candidates_completed == 1

    # A non-preemptible last operation can return after the deadline. That
    # candidate missed the publication cutoff even though decode returned.
    now[0] = 0.
    def late_return(self, encoding, **kwargs):
        trial = original(self, encoding, **kwargs)
        now[0] = 12.
        return trial
    monkeypatch.setattr(PartitionedDecoder, "decode", late_return)
    out = solve(sim, catalog, wall_clock=lambda: now[0], cpu_clock=lambda: 1.)
    assert out.best_solution is out.safe_baseline and out.stats.candidates_completed == 0


def test_existing_configs_select_clock_validation_and_scale():
    config = lambda name: M4Config.from_config(load_config("configs/" + name + ".yaml"))
    assert config("base").wall_budget_seconds == 10
    assert config("paper_large_template").wall_budget_seconds == 30
    assert config("cpu_budget_diagnostic").diagnostic_wall_safety_cap_seconds == 30
    assert config("debug_10min").validate_final and config("debug_30min").validate_final
    assert not config("paper_main").validate_final and not config("sensitivity_5h").validate_final


@pytest.mark.parametrize("full", [False, True])
def test_expiry_inside_physical_decode_stops_before_second_stop(monkeypatch, full):
    sim = setup([(0, 1, 4)], [("v", 0, 4)])
    plan = release(sim, "A", [leg("c", 0, 1, 4)])
    snap = sim.batch_snapshot()
    plans = {"A": (plan,)}
    sparse = M3Searcher(sim.router).search(snap, plans)
    actual = sim.router.shortest_time_s
    clock, queries = [0.], []
    def timed_route(origin, destination):
        queries.append((origin, destination))
        clock[0] = 10.5
        return actual(origin, destination)
    monkeypatch.setattr(sim.router, "shortest_time_s", timed_route)
    out = M4HALNS(sim.router, M4Config(noD=full)).solve(
        snap, plans, sparse, rng=Random(37), wall_clock=lambda: clock[0])
    # Only pickup deadhead and the pickup's nominal-leg check ran. A dropoff
    # would request (0, 1) a second time.
    assert queries == [(0, 0), (0, 1)]
    assert out.timing.timed_out and out.timing.wall_overrun_s == .5
    assert out.stats.candidates_started == 1 and out.stats.candidates_completed == 0
    assert out.best_solution is out.safe_baseline


def test_empty_batch_has_no_search_iterations_and_final_validation_is_timed_separately(monkeypatch):
    now = [0.]
    original = ScheduleValidator.validate
    def delayed_validation(self, result):
        original(self, result)
        now[0] += 19.
    monkeypatch.setattr(ScheduleValidator, "validate", delayed_validation)
    sim = setup([(0, 1, 1)])
    out = solve(sim, {}, M4Config(validate_final=True), wall_clock=lambda: now[0])
    assert out.best_solution is out.safe_baseline and out.best_solution.v == 0
    assert out.stats.inner_completed == out.stats.outer_completed == 0
    assert out.validation_wall_s == 19. and out.timing.wall_s == 0 and not out.timing.timed_out


def enumerate_outcomes():
    # Both parties go 0 -> 1. Cars start at 0 and 1, respectively, with capacity
    # one and three seconds per directed hop. The single seat flight lands at 2.
    outcomes = set()
    for allocation in product((None, "x", "y", "f"), repeat=2):
        if allocation.count("f") > 1:
            continue
        jobs = [i for i, res in enumerate(allocation) if res in ("x", "y")]
        for order in set(permutations(jobs * 2)):
            nodes, clock, onboard, finished = {"x": 0, "y": 1}, {"x": 0, "y": 0}, {}, {}
            for i in order:
                car = allocation[i]
                drop = onboard.get(car) == i
                target = int(drop)
                if not drop and car in onboard:
                    break
                clock[car] += 3 * abs(target - nodes[car])
                nodes[car] = target
                if drop:
                    del onboard[car]
                    finished[i] = clock[car]
                else:
                    onboard[car] = i
            else:
                durations = list(finished.values()) + [2] * allocation.count("f")
                outcomes.add((100 * len(durations), 70 * len(durations) - sum(durations) / 200))
    return outcomes


@pytest.mark.parametrize("full", (False, True))
def test_global_bound_over_plan_resource_and_sequence_assignments(full):
    sim = setup([(0, 1, 3), (1, 0, 3)], [("x", 0, 1), ("y", 1, 1)],
                [FlightInstance("f", 0, 1, 1, 2, capacity=1)])
    catalog = {}
    for rid in "AB":
        car = release(sim, rid, [leg("c", 0, 1, 3)])
        catalog[rid] = (Plan("flight", rid, (leg("a", 0, 1, 1, "FLIGHT"),)), car)
    before = sim.batch_snapshot()
    out = solve(sim, catalog, M4Config(noD=full, validate_final=True))
    outcomes = enumerate_outcomes()
    maximum = max(v for _, v in outcomes)
    assert maximum == pytest.approx(139.975)
    # Supply the independent optimizer's witness to both decoder modes.
    reference = FullDecoder(sim.router, before, catalog, M3Searcher(sim.router).search(before, catalog))
    decoder = reference if full else PartitionedDecoder(reference)
    optimum = decoder.decode(Encoding({"A": Choice("flight", ("f",)), "B": Choice("p-B", ("x",))}, "ABB"))
    assert optimum.f1 == 200 and optimum.v == pytest.approx(maximum)
    assert out.safe_baseline.v <= out.best_solution.v <= maximum + 1e-10
    assert any(out.best_solution.f1 == f1 and out.best_solution.v == pytest.approx(v)
               for f1, v in outcomes)
    assert sim.batch_snapshot() == before
