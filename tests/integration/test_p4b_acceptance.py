"""Exercise Algorithm 4 acceptance and best retention with physically decoded scores."""
from random import Random

import pytest

from src.m3 import M3Searcher
from src.m4 import Choice, Encoding
from src.m4 import halns
from tests.p4_examples import leg, release, setup


class AcceptanceRng(Random):
    """Choose the middle-distance car for S1, then supply a chosen SA variate."""
    draw = .1

    def choice(self, options):
        return 'mid' if 'mid' in options else super().choice(options)

    def random(self):
        return self.draw


def search(sim, plans, rng, **settings):
    snap = sim.batch_snapshot()
    catalog = {plan.request_id: (plan,) for plan in plans}
    candidates = M3Searcher(sim.router).search(snap, catalog)
    return halns.M4HALNS(sim.router, halns.M4Config(**settings)).solve(
        snap, catalog, candidates, rng=rng)


@pytest.mark.parametrize('no_sa', [False, True])
def test_outer_block_rewards_best_even_after_sa_backtracks(monkeypatch, no_sa):
    # Five real reachable cars with arrivals 0, 0, 100, 200, 200.
    # Dropoff times 10, 110, 210 give scores 69.95, 69.45, 68.95.
    sim = setup([(0, 1, 10), (2, 0, 100), (3, 0, 200)],
                [('near', 0, 4), ('near2', 0, 4), ('mid', 2, 4),
                 ('far', 3, 4), ('far2', 3, 4)])
    plan = release(sim, 'A', [leg('car', 0, 1, 10)])
    proposals = iter(('near', 'near2', 'far', 'far2', 'mid'))
    explored = []

    def propose(operator, decoder, current, rng, **kwargs):
        explored.append(current.choices['A'].resources[0])
        car = next(proposals)
        # The final move improves current in the SA run, despite falling below
        # the historical best. A nearly-one variate rejects it if best is used.
        rng.draw = .999999 if car == 'mid' else .1
        return Encoding({'A': Choice(plan.plan_id, (car,))}, ('A', 'A'))

    monkeypatch.setattr(halns, 'apply_operator', propose)
    out = search(sim, (plan,), AcceptanceRng(42), noS=no_sa, psi=0,
                 outer_loops_per_request=1, inner_loops=5, validate_final=True)
    assert out.best_solution.completion == {'A': 10}
    assert out.best_solution.encoding.choices['A'].resources == ('near',)
    assert out.best_solution.v == pytest.approx(69.95)
    op, = out.stats.operator_draws
    if no_sa:
        assert explored == ['mid', 'near', 'near2', 'near2', 'near2']
        assert out.current_solution.encoding.choices['A'].resources == ('near2',)
        assert out.stats.accepted_worse == 0
    else:
        assert explored == ['mid', 'near', 'near2', 'far', 'far2']
        assert out.current_solution.completion == {'A': 110}
        assert out.current_solution.v == pytest.approx(69.45)
        assert out.stats.accepted_worse == 1
    assert out.stats.rewards == 1
    assert out.state.scores[op] == pytest.approx(1.1)


@pytest.mark.parametrize('interrupt', [False, True])
def test_outer_block_rewards_multiple_best_improvements_once_if_complete(monkeypatch, interrupt):
    # S1 scores 69.45; the next two proposals score 69.70 and 69.95.
    # The last proposal would lower current to 68.95 by SA.
    sim = setup([(0, 1, 10), (2, 0, 100), (3, 0, 50), (4, 0, 200)],
                [('near', 0, 4), ('mid', 2, 4), ('closer', 3, 4), ('far', 4, 4)])
    plan = release(sim, 'A', [leg('car', 0, 1, 10)])
    proposals = iter(('closer', 'near', 'far'))
    wall = 0.

    def propose(operator, decoder, current, rng, **kwargs):
        nonlocal wall
        car = next(proposals)
        if car == 'far' and interrupt:
            wall = 10.
        return Encoding({'A': Choice(plan.plan_id, (car,))}, ('A', 'A'))

    monkeypatch.setattr(halns, 'apply_operator', propose)
    snapshot = sim.batch_snapshot()
    catalog = {'A': (plan,)}
    candidates = M3Searcher(sim.router).search(snapshot, catalog)
    cfg = halns.M4Config(psi=0, outer_loops_per_request=1, inner_loops=3)
    out = halns.M4HALNS(sim.router, cfg).solve(
        snapshot, catalog, candidates, rng=AcceptanceRng(42), wall_clock=lambda: wall
    )

    op, = out.stats.operator_draws
    assert out.best_solution.v == pytest.approx(69.95)
    assert out.timing.timed_out is interrupt
    assert out.stats.inner_completed == (2 if interrupt else 3)
    assert out.stats.outer_completed == (0 if interrupt else 1)
    assert out.stats.accepted_worse == (0 if interrupt else 1)
    assert out.stats.rewards == (0 if interrupt else 1)
    assert out.state.scores[op] == pytest.approx(1. if interrupt else 1.1)


def test_low_scoring_feasible_s1_is_explored_but_empty_baseline_wins():
    # One possible ride takes 300 minutes. Objective 70 - .3*300 = -20.
    sim = setup([(0, 1, 18000)], [('v', 0, 4)])
    plan = release(sim, 'A', [leg('car', 0, 1, 18000, ld=18420)])
    out = search(sim, (plan,), Random(21), outer_loops_per_request=1, inner_loops=2)
    assert out.stats.inner_completed == 2
    assert out.current_solution.completion == {'A': 18000}
    assert out.current_solution.v == pytest.approx(-20)
    assert out.safe_baseline.v == 0 and out.best_solution is out.safe_baseline


def test_nod_real_reverse_crosses_independent_car_fragments(monkeypatch):
    # Two locked car plans. Each component contains precisely AA or BB, so a
    # component reversal cannot change its slots; a global reversal can.
    sim = setup([(0, 1, 10)], [('x', 0, 4), ('y', 0, 4)])
    for rid, car in [('A', 'x'), ('B', 'y')]:
        plan = release(sim, rid, [leg('car', 0, 1, 10)])
        sim.accept(rid, plan, (car,))
    original = halns.apply_operator
    seen = []

    def record_reverse(*args, **kwargs):
        candidate = original(*args, **kwargs)
        seen.append(candidate.sequence)
        return candidate

    monkeypatch.setattr(halns.OperatorControl, 'choose', lambda self, rng: 'reverse')
    monkeypatch.setattr(halns, 'apply_operator', record_reverse)
    for no_d in (False, True):
        seen.clear()
        out = search(sim, (), Random(37), noD=no_d, psi=0,
                     outer_loops_per_request=1, inner_loops=10, validate_final=True)
        base_sequence = out.safe_baseline.encoding.sequence
        assert base_sequence == ('A', 'A', 'B', 'B')
        assert any(seq != base_sequence for seq in seen) == no_d
        assert out.current_solution.completion == {'A': 10, 'B': 10}
