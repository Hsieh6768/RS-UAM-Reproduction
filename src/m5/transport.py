"""Gurobi implementation of PAPER §5.4, equations (25)–(31)."""
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import product
from math import fsum, isclose, isfinite
from time import perf_counter, process_time
from types import MappingProxyType
from typing import Mapping, Optional

from .grid import Cell

Arc = tuple[Cell, Cell]
_FEASIBILITY_TOL = 1e-8
_INTEGER_TOL = 1e-8


@dataclass(frozen=True)
class GurobiConfig:
    seed: int = 0
    time_limit_s: Optional[float] = None

    def __post_init__(self):
        if type(self.seed) is not int or not 0 <= self.seed <= 2_000_000_000:
            raise ValueError("Gurobi seed must be an integer in [0, 2000000000]")
        limit = self.time_limit_s
        if limit is not None and (type(limit) not in (int, float) or not isfinite(limit) or limit <= 0):
            raise ValueError("Gurobi time_limit_s must be positive and finite")


@dataclass(frozen=True)
class TransportResult:
    mode: str
    status: str
    flow: Mapping[Arc, int] = field(default_factory=lambda: MappingProxyType({}))
    objective_m: Optional[float] = None
    status_code: Optional[int] = None
    version: Optional[tuple[int, ...]] = None
    optimize_s: Optional[float] = None
    wall_s: float = 0.
    cpu_s: float = 0.
    detail: Optional[str] = None
    error_code: Optional[int] = None


def _checked_flow(supply, demand, costs, values, objective, mode):
    flow = {}
    allocated, received = Counter(), Counter()
    for (i, j), value in values.items():
        if not isfinite(value):
            raise ValueError(f"Non-finite flow on {i} -> {j}: {value}")
        count = round(value)
        if count < 0 or abs(count - value) > _INTEGER_TOL:
            raise ValueError(f"Nonnegative integer residual on {i} -> {j}: {value}")
        if count:
            allocated[i] += count
            received[j] += count
            flow[i, j] = count
    for counts, used, require_equality in (
        (supply, allocated, mode != "SURPLUS"),
        (demand, received, mode != "SHORTAGE"),
    ):
        for cell, limit in counts.items():
            if used[cell] > limit or (require_equality and used[cell] != limit):
                raise ValueError(f"Transport constraint residual at {cell}: {used[cell]} vs {limit}")
    objective_from_flow = fsum(costs[arc] * count for arc, count in flow.items())
    if not isclose(objective, objective_from_flow, rel_tol=1e-8, abs_tol=1e-7):
        raise ValueError(f"Objective residual: Gurobi={objective}, integral flow={objective_from_flow}")
    return MappingProxyType(flow)


def solve_transport(supply, demand, cost_m, config: GurobiConfig):
    """Solve active rows/columns. Zero-supply and zero-demand arcs are forced zeros."""
    supply_total, demand_total = sum(supply.values()), sum(demand.values())
    if not supply_total or not demand_total:
        raise ValueError("Skip the solver when supply or demand is empty")
    mode = ("SHORTAGE" if supply_total < demand_total else
            "SURPLUS" if supply_total > demand_total else "BALANCED")
    start_wall, start_cpu = perf_counter(), process_time()
    result = _solve(supply, demand, cost_m, config, mode)
    return replace(result, wall_s=perf_counter() - start_wall, cpu_s=process_time() - start_cpu)


def _solve(supply, demand, cost_m, config, mode):
    try:
        import gurobipy as gp
    except ImportError as exc:
        return TransportResult(mode, "GUROBI_UNAVAILABLE", detail=str(exc))

    version = gp.gurobi.version()
    sources = sorted(i for i, count in supply.items() if count)
    targets = sorted(j for j, count in demand.items() if count)
    costs = {(i, j): cost_m(i, j) for i, j in product(sources, targets)}
    try:
        with gp.Env(empty=True) as env:
            env.setParam("OutputFlag", 0)
            env.start()
            with gp.Model("m5_transport", env=env) as model:
                return _optimize(gp, model, config, mode, version, sources, targets, costs, supply, demand)
    except gp.GurobiError as exc:
        status = "LICENSE_ERROR" if exc.errno == gp.GRB.Error.NO_LICENSE else "GUROBI_ERROR"
        return TransportResult(mode, status, version=version, detail=str(exc), error_code=exc.errno)


def _optimize(gp, model, config, mode, version, sources, targets, costs, supply, demand):
    # Stable construction and single-thread solver settings keep ties reproducible
    # for the same Gurobi release, without perturbing the PAPER objective.
    model.Params.Threads = 1
    model.Params.Seed = config.seed
    model.Params.MIPGap = 0.
    model.Params.MIPGapAbs = 0.
    model.Params.FeasibilityTol = _FEASIBILITY_TOL
    model.Params.IntFeasTol = _INTEGER_TOL
    if config.time_limit_s is not None:
        model.Params.TimeLimit = config.time_limit_s
    x = {arc: model.addVar(lb=0, vtype=gp.GRB.INTEGER, obj=cost) for arc, cost in costs.items()}
    for i in sources:
        row = gp.quicksum(x[i, j] for j in targets)
        model.addConstr(row <= supply[i] if mode == "SURPLUS" else row == supply[i])
    for j in targets:
        col = gp.quicksum(x[i, j] for i in sources)
        model.addConstr(col <= demand[j] if mode == "SHORTAGE" else col == demand[j])
    model.optimize()
    status_code = model.Status
    status_name = next((name for name in dir(gp.GRB.Status)
                        if name.isupper() and getattr(gp.GRB.Status, name) == status_code),
                       "UNKNOWN_STATUS")
    result = TransportResult(mode, status_name, status_code=status_code,
                             version=version, optimize_s=model.Runtime)
    if status_code != gp.GRB.OPTIMAL:
        return replace(result, detail=f"Gurobi did not prove optimality ({status_name}, {status_code})")
    try:
        violation = max(model.ConstrVio, model.BoundVio)
        if not isfinite(violation) or violation > _FEASIBILITY_TOL:
            raise ValueError(f"Gurobi constraint or bound residual: {violation}")
        flow = _checked_flow(supply, demand, costs, {arc: var.X for arc, var in x.items()},
                             model.ObjVal, mode)
    except ValueError as exc:
        return replace(result, status="INVALID_SOLUTION", detail=str(exc))
    return replace(result, flow=flow, objective_m=model.ObjVal)
