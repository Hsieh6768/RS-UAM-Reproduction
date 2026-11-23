"""PAPER §3.3, Algorithm 1. Single-writer rolling dispatch in simulation seconds."""
from collections import Counter
from dataclasses import replace
from itertools import count
from random import Random
from time import perf_counter, perf_counter_ns, process_time

from src.common.timebase import require_tick
from src.m1.planner import M1Planner
from src.m3.search import M3Searcher
from src.m4.halns import M4HALNS
from src.m4.task_graph import TaskGraph
from src.m5.rebalancer import M5Rebalancer
from src.simulation.engine import _record
from src.simulation.events import committed_car_ids
from src.simulation.rebalance import commit_rebalance_plan
from src.simulation.transactions import commit_passengers


TERMINAL = frozenset(("COMPLETED", "EXPIRED_UNSERVED"))


def _cohort(at, start, end):
    if at < start:
        return "WARMUP"
    return "EVALUATION" if at < end else "TAIL"


def _evaluation_open(snapshot):
    return any(r.cohort == "EVALUATION" and r.status not in TERMINAL for r in snapshot.requests.values())


class _Arrivals:
    """Look ahead by one release envelope. Payloads enter P1 only at their timestamps."""

    def __init__(self, arrivals, start, end, coverage):
        self.source = iter(arrivals)
        self.upcoming = next(self.source, None)
        self.start, self.end, self.coverage = start, end, coverage
        self.closed = False
        self.last_at = 0

    def advance(self, sim, boundary):
        released = 0
        while not self.closed and self.upcoming is not None:
            at, rid, load = self.upcoming
            require_tick(at)
            if not self.last_at <= at < self.coverage:
                raise ValueError(f"Arrival {rid} at {at} outside ordered input coverage")
            if at > boundary:
                break
            if at >= self.end:
                # The final evaluation party can finish between batches. Execute
                # all earlier events before deciding whether this release enters.
                sim.advance_to(at)
                if not _evaluation_open(sim.snapshot()):
                    self.closed = True
                    break
            cohort = _cohort(at, self.start, self.end)
            sim.schedule_arrival(rid, at, lambda load=load, c=cohort: replace(load(), cohort=c))
            self.last_at = at
            released += 1
            self.upcoming = next(self.source, None)
        if not self.closed and boundary >= self.coverage:
            sim.advance_to(self.coverage)
            if _evaluation_open(sim.snapshot()):
                raise ValueError(f"Demand coverage exhausted at {self.coverage} with evaluation requests outstanding")
            self.closed = True
        sim.advance_to(boundary)
        if not self.closed and boundary >= self.end and not _evaluation_open(sim.snapshot()):
            self.closed = True
        return released


class _Timetable:
    """D011. Stable P1 route/index IDs share one seat inventory for the run."""

    def __init__(self, air):
        self.air = air
        self.indices = {rid: 0 for rid in sorted(air.routes)}
        self.generated_through = -1

    def extend(self, sim, until):
        if until <= self.generated_through:
            return False
        added = False
        for rid, index in self.indices.items():
            headway = self.air.routes[rid].headway_s
            last = (until - self.air.phase_s) // headway
            for slot in range(index, last + 1):
                sim.add_flight(self.air.flight(rid, slot))
                added = True
            self.indices[rid] = max(index, last + 1)
        self.generated_through = until
        return added


def _count_requests(snap):
    counts = Counter()
    for req in snap.requests.values():
        counts["accepted"] += req.accepted_at is not None
        counts["expired"] += req.status == "EXPIRED_UNSERVED"
        counts["pending"] += req.status == "WAITING"
        counts["accepted_incomplete"] += req.accepted_at is not None and req.status != "COMPLETED"
        if req.cohort == "EVALUATION":
            counts["evaluation_accepted"] += req.accepted_at is not None
            counts["evaluation_completed"] += req.status == "COMPLETED"
    return counts


def _fleet(snap):
    committed = committed_car_ids(snap.tasks.values())
    idle = [vid for vid, v in snap.vehicles.items() if v.parked_without_program and vid not in committed]
    loaded = sum(bool(v.onboard) for v in snap.vehicles.values())
    moving = sum(v.rebalance_target is not None for v in snap.vehicles.values())
    pickup = len(snap.vehicles) - len(idle) - loaded - moving
    return idle, dict(fleet_idle=len(idle), fleet_loaded=loaded, fleet_rebalancing=moving,
                      fleet_pickup=pickup, fleet_busy=loaded + pickup)


def _solver_metrics(result, plan):
    stats, best, timing = result.stats, result.best_solution, result.timing
    graph = TaskGraph.from_tasks(enumerate(t.task for t in best.tasks), best.encoding.choices, lambda: None)
    solver = plan.transport
    return dict(
        m4_wall_ms=1000 * timing.wall_s, m4_cpu_ms=1000 * timing.cpu_s,
        m4_wall_overrun_ms=1000 * timing.wall_overrun_s,
        budget_exhausted=timing.timed_out, m4_stop_reason=timing.stop_reason,
        m4_wall_budget_exceeded=timing.wall_s > result.config.wall_budget_seconds,
        candidates_evaluated=stats.candidates_completed, candidates_started=stats.candidates_started,
        decode_count=result.safe_baseline.attempts + stats.candidates_completed + sum(stats.repair_reasons.values()),
        invalid_candidates=stats.invalid_candidates, repair_count=sum(stats.repair_reasons.values()),
        repair_reasons=dict(stats.repair_reasons), failure_reasons=dict(stats.failure_reasons),
        graph_components=len(graph.components), largest_component_tasks=max(
            (len(comp.tasks) for comp in graph.components), default=0),
        m4_tasks=len(best.tasks), evaluated_components=stats.evaluated_components,
        reused_components=stats.reused_components, component_size_counts=dict(stats.component_size_counts),
        operator_draws=dict(Counter(stats.operator_draws)), operator_scores=dict(result.state.scores),
        sa_accept_worse_count=stats.accepted_worse, operator_rewards=stats.rewards,
        best_score=best.v, used_fallback=(best is result.safe_baseline),
        validator_enabled=result.config.validate_final, validator_wall_ms=1000 * result.validation_wall_s,
        m5_wall_ms=1000 * plan.wall_s, m5_cpu_ms=1000 * plan.cpu_s,
        m5_status=plan.status, m5_reason=plan.reason, m5_supply=plan.supply_total,
        m5_demand=plan.demand_total, m5_uncovered=sum(plan.uncovered_demand.values()),
        m5_local_retained=len(plan.local_vehicle_ids), m5_route_failure=_record(plan.route_failure),
        m5_flow=(_record(solver.flow) if solver else None),
        m5_solver_status=solver.status if solver else None, m5_detail=solver.detail if solver else None,
        m5_solver_wall_ms=1000 * solver.wall_s if solver else 0,
        m5_optimize_s=solver.optimize_s if solver else None,
        m5_placement_ms=1000 * plan.placement_wall_s)


def run_rolling(sim, network, arrivals, *, eval_start, eval_end, coverage_end, batch_seconds,
                seed, m1_config, m3_config, m4_config, m5_config, reporter, failure_dir,
                validator_enabled=None, planning_network=None, profile_run=None, diagnostic_run=None):
    """Run P1–P5 through demand closure. The experiment owner handles files/errors.

    Ticks are relative to warmup start. `coverage_end` is the exclusive end of
    continuous P0 demand coverage, independent of the last sampled request.
    `arrivals` supplies sorted (tick, unique ID, deferred Request loader) tuples.
    """
    for tick in (eval_start, eval_end, coverage_end, batch_seconds):
        require_tick(tick)
    if sim.now != 0 or not 0 <= eval_start < eval_end <= coverage_end or not batch_seconds:
        raise ValueError("Invalid experiment window, interval, or demand coverage")
    if sim.router.version != network.road.version:
        raise ValueError("Simulation and planning road identities differ")
    if planning_network is None:
        planning_network = network
    if planning_network.road.version != sim.router.version:
        raise ValueError("M1 planning road differs from simulator road")
    if profile_run is not None and diagnostic_run is not None:
        raise ValueError("Request diagnostics cannot run with legacy profiling")
    if validator_enabled is not None:
        m4_config = replace(m4_config, validate_final=validator_enabled)
    planner, searcher = M1Planner(m1_config), M3Searcher(network.road, m3_config)
    halns, rebalancer = M4HALNS(network.road, m4_config), M5Rebalancer(network.road, m5_config)
    source = _Arrivals(arrivals, eval_start, eval_end, coverage_end)
    flights = _Timetable(network.air)
    rng = Random(seed)
    plans, previous_counts = {}, Counter()
    for batch in count(1):
        started = perf_counter()
        tick = (batch - 1) * batch_seconds
        flights.extend(sim, tick)
        new_count = source.advance(sim, tick)
        before = sim.batch_snapshot()
        types = before.request_types(tick - batch_seconds if batch > 1 else None)
        trace = diagnostic_run.begin(batch, before, types, source.closed) if diagnostic_run is not None else None
        if trace is not None:
            diagnostic_start = perf_counter_ns()
            cached = trace.targets.intersection(plans)
            previous, rng_state = sim.search_state, rng.getstate()
            diagnostic_setup_ns = perf_counter_ns() - diagnostic_start
        waiting = {rid for rid, kind in types.items() if kind != 3 and not source.closed}
        # An explicit per-active-request cache remains valid even when the
        # planner's bounded cache evicts an option from an unusually large batch.
        m1_start = perf_counter()
        hits = len(waiting.intersection(plans))
        plans = {rid: (plans[rid] if rid in plans else planner.generate(before.requests[rid], planning_network))
                 for rid in sorted(waiting)}
        m1_wall_ms = 1000 * (perf_counter() - m1_start)
        # Include every service a currently released party could legally use,
        # including flights after the experiment's configured evaluation end.
        last = max((int(before.requests[rid].final_deadline) for rid in waiting), default=tick)
        if flights.extend(sim, last):
            before = sim.batch_snapshot()
        sparse = searcher.search(before, plans, diagnostic=trace)
        basic = diagnostic_run.begin_basic(tick, plans, sparse) if diagnostic_run is not None else None
        profile = profile_run.begin(batch) if profile_run is not None else None
        if profile is not None:
            profile.measure("request_sampling", profile.select_requests, before, plans, sparse)
        m4_start_wall, m4_start_cpu = perf_counter(), process_time()
        result = halns.solve(before, plans, sparse, rng=rng, previous=sim.search_state, failure_dir=failure_dir,
                             profile=profile, diagnostic=trace,
                             observe_candidate=basic.decoded if basic is not None else None)
        m4_call_wall_ms = 1000 * (perf_counter() - m4_start_wall)
        m4_call_cpu_ms = 1000 * (process_time() - m4_start_cpu)
        if profile is None:
            commit_passengers(sim, before, plans, result)
        else:
            profile.measure("passenger_commit", commit_passengers, sim, before, plans, result)
        post = sim.batch_snapshot()
        if basic is not None:
            diagnostic_run.record_basic(basic)
        if trace is not None:
            diagnostic_run.write(trace, before, post, types, plans, cached, sparse, result, previous, rng_state,
                                 planner.config, m3_config, planning_network.air,
                                 source.closed, diagnostic_setup_ns)
        idle, _ = _fleet(post)
        unmet = () if source.closed else (rid for rid, req in post.requests.items() if req.status == "WAITING")
        plan = rebalancer.propose(post, unmet, idle, failure_dir=failure_dir)
        if profile is None:
            moved = commit_rebalance_plan(sim, post, plan)
        else:
            moved = profile.measure("rebalance_commit", commit_rebalance_plan, sim, post, plan)
        after = sim.batch_snapshot()
        counts = _count_requests(after)
        _, fleet = _fleet(after)
        classified = Counter(types.values())
        metrics = dict(
            batch=batch, batch_time=tick, cohort=_cohort(tick, eval_start, eval_end),
            intake_closed=source.closed, new_req=new_count, type1=classified[1], type2=classified[2],
            type3=classified[3], accepted_new=counts["accepted"] - previous_counts["accepted"],
            expired=counts["expired"] - previous_counts["expired"], pending_after=counts["pending"],
            accepted_incomplete=counts["accepted_incomplete"], evaluation_accepted=counts["evaluation_accepted"],
            evaluation_completed=counts["evaluation_completed"], flights_reserved=sum(
                f.reserved_seats for f in after.flights.values()), m1_wall_ms=m1_wall_ms,
            m1_planned=len(waiting) - hits, m1_cache_hits=hits, m1_active_cache_size=len(plans),
            m3_wall_ms=1000 * sparse.stats.total_s, m3_index_ms=1000 * sparse.stats.index_build_s,
            m3_car_queries=sparse.stats.car_queries, m3_car_legs_truncated=sparse.stats.car_legs_truncated,
            m3_flight_candidates=sparse.stats.flight_candidates, rebalanced=moved, **fleet,
            m4_call_wall_ms=m4_call_wall_ms, m4_call_cpu_ms=m4_call_cpu_ms,
            **_solver_metrics(result, plan), **sim.storage_metrics())
        for name, cache in (("distance", network.road._distance), ("path", network.road._path)):
            info = cache.cache_info()
            metrics.update({f"road_{name}_hits": info.hits, f"road_{name}_misses": info.misses,
                            f"road_{name}_size": info.currsize})
        events = sim.drain_history()
        reporter.events(events)
        metrics.update(batch_event_count=len(events), request_records=len(after.requests),
                       task_records=len(after.tasks), flight_records=len(after.flights))
        metrics["batch_wall_ms"] = (perf_counter() - started) * 1000
        metrics["batch_wall_budget_exceeded"] = metrics["batch_wall_ms"] > 1000 * m4_config.wall_budget_seconds
        if profile is not None:
            profile_run.write(profile, after, result, plan, moved)
        reporter.batch(metrics)
        previous_counts = counts
        if source.closed and not (counts["pending"] or counts["accepted_incomplete"]
                                  or fleet["fleet_rebalancing"]):
            if counts["evaluation_accepted"] != counts["evaluation_completed"]:
                raise RuntimeError("Evaluation acceptance/completion mismatch at closure")
            return after
