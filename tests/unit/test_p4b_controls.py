"""Hand-calculated operator, SA, and dual-clock examples (D007/D014)."""
from random import Random

import pytest

from src.m4 import budget, control


class Draws(Random):
    """Prescribe the unit-uniform draws used by random() and uniform()."""
    def __init__(self, *values):
        self.values = iter(values)

    def random(self):
        return next(self.values)


class Clocks:
    wall = 100.0
    cpu = 20.0

    def start(self, **options):
        return budget.SearchBudget(
            wall_clock=lambda: self.wall, cpu_clock=lambda: self.cpu, **options
        )


def test_roulette_initial_weights_boundaries_and_batch_inheritance():
    first = control.OperatorControl(psi=0)
    assert tuple(first.scores.values()) == (1.0,) * 7
    assert first.choose(Draws(.001)) == 'p_exchange'
    assert first.choose(Draws(.15)) == 'v_exchange'
    assert first.choose(Draws(.999)) == 'reverse'
    assert first.update('p_exchange', 5, 6, Random(2))
    assert first.scores['p_exchange'] == pytest.approx(1.1)
    assert not first.update('p_exchange', 6, 6, Random(2))
    assert not first.update('p_exchange', 6, 5, Random(2))
    second = control.OperatorControl(first.scores, psi=0)
    assert second.choose(Draws(.15)) == 'p_exchange'
    second.update('reverse', 0, 1, Random(2))
    assert first.scores['reverse'] == 1


def test_equation_24_each_operator_has_independent_gate_signed_step_and_floor():
    weights = control.OperatorControl(initial_score=.01, floor=.005)
    # operator 1: reward .1, then -.02; 2: +.01; 3: gate at psi;
    # 4: -.02 floors .01 to .005; 5-7: no mutation.
    weights.update('p_exchange', 7, 8, Draws(0, 0, 0, .75, .4, 0, 0, .6, .7, .8))
    assert tuple(weights.scores.values()) == pytest.approx(
        (.09, .02, .01, .005, .01, .01, .01)
    )


def test_noa_keeps_equal_weights_even_with_adaptive_prior():
    prior = dict.fromkeys(control.OPERATORS, 1.)
    prior['v_exchange'] = 91
    weights = control.OperatorControl(prior, adaptive=False)
    assert weights.choose(Draws(.4)) == 'restart'
    assert not weights.update('restart', 1, 2, Draws())
    assert tuple(weights.scores.values()) == (1.,) * 7


def test_sa_compares_with_current_and_never_accepts_infeasible():
    accept = control.accept_candidate
    assert accept(9, 8, 0, Draws())
    assert accept(8, 8, 0, Draws(), annealing=False)
    assert not accept(None, 8, 100, Draws(), feasible=False)
    # exp((8 - 10) / 2) = 0.367879...
    assert accept(8, 10, 2, Draws(.3678))
    assert not accept(8, 10, 2, Draws(.368))
    assert not accept(8, 10, 2, Draws(), annealing=False)
    assert not accept(8, 10, 0, Draws())
    assert not accept(8, 10, 5e-324, Draws(0))


@pytest.mark.parametrize('limit', (10., 30.))
def test_formal_deadline_uses_wall_even_if_cpu_clock_reaches_limit_first(limit):
    clock = Clocks()
    timer = clock.start(wall_budget_seconds=limit)
    clock.cpu += limit
    clock.wall += limit - 1
    timer.checkpoint()
    clock.wall += 1
    with pytest.raises(budget.BudgetExpired):
        timer.checkpoint()
    clock.wall += .25
    timing = timer.finish()
    assert timing.timed_out and timing.stop_reason == 'wall_deadline'
    assert (timing.wall_s, timing.cpu_s, timing.wall_overrun_s) == (limit+.25, limit, .25)
    assert timing.cpu_overrun_s == 0


def test_supplied_absolute_deadline_and_expiry_during_final_work():
    clock = Clocks()
    timer = clock.start(wall_deadline=104)
    clock.wall = 105
    timing = timer.finish()
    assert timing.timed_out and timing.wall_s == 5 and timing.wall_overrun_s == 1


def test_cpu_diagnostic_requires_wall_cap_and_stops_on_either_clock():
    clock = Clocks()
    with pytest.raises(ValueError):
        clock.start(budget_clock='cpu', cpu_budget_seconds=10)
    options = dict(budget_clock='cpu', cpu_budget_seconds=10,
                   diagnostic_wall_safety_cap_seconds=30)
    timer = clock.start(**options)
    clock.wall += 12
    timer.checkpoint()
    clock.cpu += 10.5
    with pytest.raises(budget.BudgetExpired):
        timer.checkpoint()
    assert timer.finish().stop_reason == 'cpu_deadline'
    assert timer.finish().cpu_overrun_s == .5

    timer = clock.start(**options)
    clock.wall += 31
    assert timer.finish().stop_reason == 'wall_safety_cap'
    assert timer.finish().wall_overrun_s == 1


def test_aggregate_measures_actual_wall_cpu_timeout_and_overshoot():
    records = []
    clock = Clocks()
    for wall, cpu in ((1, .5), (2, 1), (3, 1.5), (12, 2)):
        timer = clock.start()
        clock.wall += wall
        clock.cpu += cpu
        records.append(timer.finish())
    stats = budget.aggregate_timings(records)
    assert stats['batches'] == 4 and stats['timeout_rate'] == .25
    assert stats['wall_s'] == pytest.approx(dict(mean=4.5, p95=10.65, p99=11.73, max=12))
    assert stats['cpu_s'] == pytest.approx(dict(mean=1.25, p95=1.925, p99=1.985, max=2))
    assert stats['max_wall_overrun_s'] == 2
    assert budget.aggregate_timings([])['wall_s']['p95'] is None
