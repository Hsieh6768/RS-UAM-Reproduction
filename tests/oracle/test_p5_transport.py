from itertools import product
from math import inf

import pytest


def enumerate_flows(supply, demand, costs):
    """Independent exact enumeration of every bounded matrix entry."""
    sources, targets = tuple(supply), tuple(demand)
    pairs = tuple(product(sources, targets))
    supply_total, demand_total = sum(supply.values()), sum(demand.values())
    best = inf
    for values in product(*(range(1 + min(supply[i], demand[j])) for i, j in pairs)):
        rows = dict(zip(pairs, values))
        row_used = {i: sum(rows[i, j] for j in targets) for i in sources}
        col_used = {j: sum(rows[i, j] for i in sources) for j in targets}
        if any(row_used[i] > supply[i] for i in sources):
            continue
        if any(col_used[j] > demand[j] for j in targets):
            continue
        if sum(values) == min(supply_total, demand_total):
            best = min(best, sum(rows[pair] * costs[pair] for pair in pairs))
    return best


@pytest.mark.parametrize(("mode", "supplies", "demands", "known"), [
    ("SHORTAGE", (1, 0, 0, 0), (0, 2, 0, 0), 80),
    ("SHORTAGE", (1, 0, 1, 0), (1, 2, 0, 0), 80),
    ("SURPLUS", (2, 1, 0, 2), (0, 1, 1, 0), 100),
    ("BALANCED", (2, 0, 1, 0), (1, 0, 0, 2), 250),
    ("BALANCED", (1, 0, 1, 0), (0, 2, 0, 0), 160),
])
def test_p5_gurobi_against_all_integer_matrices(mode, supplies, demands, known):
    from src.m5.transport import GurobiConfig, solve_transport

    cells = ((0, 0), (1, 0), (2, 0), (4, 0))
    a = {c: n for c, n in zip(cells, supplies) if n}
    b = {c: n for c, n in zip(cells, demands) if n}
    # Independent synthetic symmetric grid costs.
    costs = dict(zip(product(cells, cells), (
        0, 80, 120, 150, 80, 0, 80, 85, 120, 80, 0, 100, 150, 85, 100, 0,
    )))
    expected = enumerate_flows(a, b, costs)
    assert expected == known
    result = solve_transport(a, b, lambda i, j: costs[i, j], GurobiConfig(time_limit_s=2))
    assert (result.mode, result.status, result.objective_m) == (mode, "OPTIMAL", expected)
    assert result.version and result.status_code == 2
    assert all(type(qty) is int and qty > 0 for qty in result.flow.values())
    for i, quantity in a.items():
        used = sum(q for (u, _), q in result.flow.items() if u == i)
        assert used <= quantity if mode == "SURPLUS" else used == quantity
    for j, quantity in b.items():
        used = sum(q for (_, v), q in result.flow.items() if v == j)
        assert used <= quantity if mode == "SHORTAGE" else used == quantity
    assert sum(result.flow.values()) == min(sum(a.values()), sum(b.values()))


def test_p5_equal_optima_stable_under_reordered_inputs():
    from src.m5.transport import GurobiConfig, solve_transport

    a = {(0, 0): 1, (2, 0): 1}
    b = {(1, 1): 1, (1, -1): 1}
    cfg = GurobiConfig(time_limit_s=2)
    first = solve_transport(a, b, lambda *_: 1.0, cfg)
    second = solve_transport(dict(reversed(list(a.items()))), dict(reversed(list(b.items()))),
                             lambda *_: 1.0, cfg)
    assert first.objective_m == second.objective_m == 2
    assert tuple(first.flow.items()) == tuple(second.flow.items())


@pytest.mark.parametrize(("values", "objective"), [([0, 1], 2), ([1, 1], 3),
                                                    ([0.5, 0.5], 1.5), ([1, 0], 2)])
def test_p5_reject_invalid_solver_residuals(values, objective):
    from src.m5.transport import _checked_flow

    row, col, extra = (0, 0), (1, 0), (2, 0)
    costs = {(row, col): 1., (row, extra): 2.}
    with pytest.raises(ValueError):
        _checked_flow({row: 1}, {col: 1, extra: 0}, costs, dict(zip(costs, values)),
                      objective, "SURPLUS")


def test_p5_reports_gurobi_license_exception_without_incumbent(monkeypatch):
    import gurobipy as gp

    from src.m5.transport import GurobiConfig, solve_transport

    def denied(*args, **kwargs):
        raise gp.GurobiError(gp.GRB.Error.NO_LICENSE, "synthetic checkout failure")

    monkeypatch.setattr(gp, "Env", denied)
    result = solve_transport({(0, 0): 1}, {(1, 0): 1}, lambda *_: 1., GurobiConfig())
    assert result.status == "LICENSE_ERROR"
    assert result.error_code == gp.GRB.Error.NO_LICENSE
    assert result.detail == "synthetic checkout failure" and result.objective_m is None
    assert result.flow == {} and result.status_code is None
