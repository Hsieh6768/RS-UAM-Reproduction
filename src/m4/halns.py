"""PAPER Algorithm 4 with D004–D007 rolling commitments and D014 timing."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from math import isfinite
from time import perf_counter, process_time
from typing import Mapping, Optional

from .baseline import BaselineOrderRequired, SnapshotError, record_failure
from .budget import BudgetExpired, BudgetTiming, SearchBudget
from .control import OperatorControl, accept_candidate
from .decoder import FullDecoder
from .encoding import Choice, EncodingError, committed_choice, frozen, task_chain
from .operators import apply_operator
from .partitioned_decoder import PartitionedDecoder
from .result import DecodedSchedule
from .task_graph import TaskGraph
from .validation import ScheduleValidator


@dataclass(frozen=True)
class M4Config:
    outer_loops_per_request: int = 2
    inner_loops: int = 25
    exchange_request_count: int = 1
    initial_temperature_per_request: float = 70.
    cooling_rate: float = .7
    beta: float = .7
    delta: float = 100.
    initial_score: float = 1.
    theta: float = .1
    psi: float = .4
    omega: float = .02
    floor: float = 1e-9
    noD: bool = False
    noA: bool = False
    noS: bool = False
    validate_final: bool = False
    wall_budget_seconds: float = 10.
    budget_clock: str = "wall"
    cpu_budget_seconds: Optional[float] = None
    diagnostic_wall_safety_cap_seconds: Optional[float] = None

    def __post_init__(self):
        for name in ("outer_loops_per_request", "inner_loops", "exchange_request_count"):
            n = getattr(self, name)
            if type(n) is not int or n < (1 if name == "exchange_request_count" else 0):
                raise ValueError(f"Invalid {name}: {n}")
        if (not isfinite(self.initial_temperature_per_request) or self.initial_temperature_per_request < 0
                or not 0 < self.cooling_rate <= 1):
            raise ValueError("Invalid temperature or cooling rate")

    @classmethod
    def from_config(cls, config):
        """Read an already merged project configuration (src.common.config)."""
        m = config["m4"]
        if m["decoder"] not in ("partitioned", "full") or not m["cool_after_each_inner_iteration"]:
            raise ValueError("Unsupported M4 decoder or temperature schedule")
        if m["objective_travel_time_unit"] != "minutes" or not m["record_cpu_time"]:
            raise ValueError("M4 requires minutes for scoring and both clocks for reporting")
        return cls(
            outer_loops_per_request=m["outer_loops_per_request"], inner_loops=m["inner_loops"],
            exchange_request_count=m["exchange_request_count"],
            initial_temperature_per_request=m["initial_temperature_per_request"], cooling_rate=m["cooling_rate"],
            beta=m["objective_beta"], delta=m["objective_fulfillment_reward"],
            initial_score=m["operator_initial_score"], theta=m["operator_reward_theta"],
            psi=m["mutation_probability_psi"], omega=m["mutation_magnitude_omega"],
            floor=m["roulette_positive_floor"], noD=m["decoder"] == "full",
            noA=not m["dynamic_operator_scoring"], noS=not m["simulated_annealing"],
            validate_final=config["validation"]["schedule_validator_enabled"],
            budget_clock=m["budget_clock"], wall_budget_seconds=m["wall_budget_seconds"],
            cpu_budget_seconds=m.get("cpu_budget_seconds"),
            diagnostic_wall_safety_cap_seconds=m.get("diagnostic_wall_safety_cap_seconds"))


@dataclass(frozen=True)
class SearchState:
    """Carry the returned best schedule's stable references, never its score."""
    scores: Mapping[str, float]
    rng_state: tuple
    remaining_task_ids: tuple[str, ...]
    choices: Mapping[str, Choice]
    graph_version: str
    batch_time: int

    def remaining_sequence(self, snapshot):
        if self.graph_version != snapshot.graph_version or self.batch_time > snapshot.time:
            raise SnapshotError("Prior search state belongs to a different graph or a future batch")
        owners = {}
        ordered = {}
        for rid, kind in snapshot.request_types().items():
            if kind != 3:
                continue
            req = snapshot.requests[rid]
            if req.plan is None or len(req.bindings) != len(req.plan.legs):
                raise SnapshotError(f"Missing fixed plan or bindings for {rid}")
            choice = committed_choice(req)
            if self.choices.get(rid) != choice:
                raise BaselineOrderRequired(f"Prior selection missing or changed for committed request {rid}")
            pending = []
            for t in task_chain(req.plan, choice):
                real = snapshot.tasks.get(t.task_id)
                if real is None:
                    raise SnapshotError(f"Missing committed task {t.task_id}")
                if t.resource_id is not None and real.status == "PENDING":
                    owners[t.task_id] = rid
                    pending.append(t.task_id)
            ordered[rid] = pending
        sequence, seen = [], defaultdict(list)
        for tid in self.remaining_task_ids:
            if tid in owners:
                rid = owners[tid]
                sequence.append(rid)
                seen[rid].append(tid)
        for rid, expected in ordered.items():
            if seen[rid] != expected:
                raise BaselineOrderRequired(f"Incomplete or unordered prior tasks for {rid}: "
                                            f"expected {expected}, received {seen[rid]}")
        return tuple(sequence)


@dataclass(frozen=True)
class SearchStats:
    request_count: int
    outer_limit: int
    outer_completed: int
    inner_completed: int
    candidates_started: int  # includes S1, excludes S0
    candidates_completed: int
    invalid_candidates: int
    accepted_worse: int
    operator_draws: tuple[str, ...]
    rewards: int
    repair_reasons: Mapping[str, int]
    failure_reasons: Mapping[str, int]
    component_size_counts: Mapping[int, int]
    evaluated_components: int
    reused_components: int
    final_temperature: float


@dataclass(frozen=True)
class SearchResult:
    best_solution: DecodedSchedule
    current_solution: DecodedSchedule
    safe_baseline: DecodedSchedule
    initial_scores: Mapping[str, float]
    state: SearchState
    stats: SearchStats
    timing: BudgetTiming
    config: M4Config
    validation_wall_s: float


class M4HALNS:
    def __init__(self, road, config=M4Config()):
        self.road = road
        self.config = config

    def solve(self, snapshot, plans, candidates, *, rng, previous=None, remaining_sequence=None,
              failure_dir="outputs/p4b_failures", wall_clock=perf_counter, cpu_clock=process_time,
              wall_deadline=None):
        """Evaluate one frozen batch. The caller owns any subsequent commit."""
        cfg = self.config
        budget = SearchBudget(wall_budget_seconds=cfg.wall_budget_seconds, budget_clock=cfg.budget_clock,
                              cpu_budget_seconds=cfg.cpu_budget_seconds,
                              diagnostic_wall_safety_cap_seconds=cfg.diagnostic_wall_safety_cap_seconds,
                              wall_clock=wall_clock, cpu_clock=cpu_clock, wall_deadline=wall_deadline)
        if previous is not None:
            if remaining_sequence is not None:
                raise ValueError("Pass previous search state or an explicit remaining sequence")
            try:
                remaining_sequence = previous.remaining_sequence(snapshot)
            except SnapshotError as exc:
                exc.failure_path = record_failure(failure_dir, snapshot, exc)
                raise
        # S0 is mandatory: charge its actual duration, even if proving S0 alone
        # overruns the deadline. There is no previously scored fallback in this batch.
        try:
            full = FullDecoder(self.road, snapshot, plans, candidates, beta=cfg.beta, delta=cfg.delta,
                               failure_dir=failure_dir, remaining_sequence=remaining_sequence)
        except EncodingError as exc:
            exc.failure_path = record_failure(failure_dir, snapshot, exc)
            raise
        if previous is not None:
            rng.setstate(previous.rng_state)
        operators = OperatorControl(None if previous is None else previous.scores, adaptive=not cfg.noA,
                                    initial_score=cfg.initial_score, theta=cfg.theta, psi=cfg.psi,
                                    omega=cfg.omega, floor=cfg.floor)
        initial_scores = frozen(operators.scores)
        count = len(full.fixed_choices) + sum(
            req.status == "WAITING" and req.submitted_at <= snapshot.time <= req.pickup_deadline
            for req in snapshot.requests.values())
        limit = cfg.outer_loops_per_request * count
        temperature = cfg.initial_temperature_per_request * count
        partitioned = None if cfg.noD else PartitionedDecoder(full)
        evaluator = partitioned or full
        safe = current = best = full.safe_baseline
        started = completed = invalid = worse = inner = outer = rewards = 0
        evaluated_components = reused_components = 0
        repairs, failures, sizes = Counter(), Counter(), Counter()
        draws = []
        # The current encoding determines the scopes. Reuse until an accepted
        # candidate changes the encoding (including deletion during repair).
        scope_encoding, fragments = None, ()

        def evaluate(encoding):
            nonlocal completed, invalid, evaluated_components, reused_components
            trial = evaluator.decode(encoding, checkpoint=budget.checkpoint)
            budget.checkpoint()
            completed += 1
            repairs.update(r.reason for r in trial.repairs)
            if trial.failure:
                invalid += 1
                failures[trial.failure.reason] += 1
            if partitioned is not None:
                stats = partitioned.last_stats
                sizes.update(stats.component_sizes)
                evaluated_components += stats.evaluated_components
                reused_components += stats.reused_components
            return trial

        try:
            budget.checkpoint()
            started += 1
            initial = evaluate(full.initial_encoding(rng, checkpoint=budget.checkpoint))
            if initial.feasible:
                current = initial
                if initial.better_than(best):
                    best = initial
            for _ in range(limit):
                budget.checkpoint()
                old_best = best.v
                op = operators.choose(rng)
                draws.append(op)
                for _ in range(cfg.inner_loops):
                    budget.checkpoint()
                    if op in ("insert", "cut", "swap", "reverse") and scope_encoding != current.encoding:
                        if cfg.noD:
                            fragments = (range(len(current.encoding.sequence)),)
                        else:
                            graph = TaskGraph.build(full, current.encoding, checkpoint=budget.checkpoint)
                            fragments = tuple(c.positions for c in graph.components)
                        scope_encoding = current.encoding
                    budget.checkpoint()
                    started += 1
                    encoding = apply_operator(op, full, current.encoding, rng, fragments=fragments,
                                              m=cfg.exchange_request_count, checkpoint=budget.checkpoint)
                    budget.checkpoint()
                    trial = evaluate(encoding)
                    if accept_candidate(trial.v, current.v, temperature, rng,
                                        feasible=trial.feasible, annealing=not cfg.noS):
                        worse += int(trial.v < current.v)
                        current = trial
                        if trial.better_than(best):
                            best = trial
                    temperature *= cfg.cooling_rate
                    inner += 1
                rewards += operators.update(op, old_best, current.v, rng)
                outer += 1
        except BudgetExpired:
            pass
        state = SearchState(frozen(operators.scores), rng.getstate(),
                            tuple(t.task.task_id for t in best.tasks), best.encoding.choices,
                            snapshot.graph_version, snapshot.time)
        stats = SearchStats(count, limit, outer, inner, started, completed, invalid, worse, tuple(draws),
                            rewards, frozen(repairs), frozen(failures), frozen(sizes), evaluated_components,
                            reused_components, temperature)
        timing = budget.finish()
        validation_wall_s = 0.
        if cfg.validate_final:
            start = wall_clock()
            ScheduleValidator(full, failure_dir=failure_dir).validate(best)
            validation_wall_s = wall_clock() - start
        return SearchResult(best, current, safe, initial_scores, state, stats, timing, cfg, validation_wall_s)
