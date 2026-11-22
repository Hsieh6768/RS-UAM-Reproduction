"""Bounded, deterministic observation of a single-writer rolling run."""
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from hashlib import blake2b
from heapq import nsmallest
import json
from time import perf_counter_ns

from src.m2.passengers import Leg
from src.m4.control import OPERATORS


@dataclass(frozen=True)
class ProfileConfig:
    enabled: bool = False
    batch_every: int = 10
    candidate_every: int = 32
    max_candidates_per_batch: int = 8
    max_detail_spans_per_batch: int = 512
    max_requests_per_batch: int = 3
    max_output_bytes: int = 4 * 1024 * 1024

    @classmethod
    def from_config(cls, config):
        try:
            return cls(**config.get("profiling", {}))
        except TypeError as exc:
            raise ValueError(f"Invalid profiling settings: {exc}") from exc

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("profiling.enabled must be boolean")
        for name, ceiling in (("batch_every", 1000000), ("candidate_every", 1000000),
                              ("max_candidates_per_batch", 10000), ("max_detail_spans_per_batch", 100000),
                              ("max_requests_per_batch", 64), ("max_output_bytes", 1024**3)):
            value = getattr(self, name)
            minimum = 0 if name == "max_requests_per_batch" else 1
            if type(value) is not int or not minimum <= value <= ceiling:
                raise ValueError(f"profiling.{name} must be an integer in [{minimum}, {ceiling}]")


class BatchProfile:
    def __init__(self, config, batch, *, clock=perf_counter_ns):
        self.config = config
        self.batch = batch
        self.clock = clock
        self.stages = {}
        self.stack = []
        self.detail_spans = 0
        self.detail_spans_truncated = 0
        self.operator_calls = dict.fromkeys(OPERATORS, 0)
        self.requests = {}
        self.requests_eligible = 0
        self.sampled_candidates = 0

    @contextmanager
    def span(self, name):
        stats = self.stages.setdefault(name, {"calls": 0, "inclusive_ns": 0, "exclusive_ns": 0})
        self.stack.append(0)
        start = self.clock()
        try:
            yield
        finally:
            elapsed = self.clock() - start
            children = self.stack.pop()
            stats["calls"] += 1
            stats["inclusive_ns"] += elapsed
            stats["exclusive_ns"] += elapsed - children
            if self.stack:
                self.stack[-1] += elapsed

    def measure(self, name, fn, *args, **kwargs):
        with self.span(name):
            return fn(*args, **kwargs)

    def _limited(self, name, fn, *args, **kwargs):
        if self.detail_spans >= self.config.max_detail_spans_per_batch:
            self.detail_spans_truncated += 1
            return fn(*args, **kwargs)
        self.detail_spans += 1
        return self.measure(name, fn, *args, **kwargs)

    def detail(self, name, fn):
        return partial(self._limited, name, fn)

    def timer(self, name, fn):
        return partial(self.measure, name, fn)

    def samples_candidate(self, ordinal):
        stride = self.config.candidate_every
        return (ordinal - 1) % stride == 0 and (ordinal - 1) // stride < self.config.max_candidates_per_batch

    def select_requests(self, snapshot, plans, sparse):
        def rank(rid):
            key = f"{self.batch}/{rid}".encode("utf-8")
            return blake2b(key, digest_size=8).digest(), rid
        self.requests_eligible = len(plans)
        for rid in nsmallest(self.config.max_requests_per_batch, plans, key=rank):
            rows = []
            for plan in plans[rid]:
                legs = [{"leg_id": leg.leg_id, "mode": leg.mode.value,
                         "count": len(sparse.by_leg.get((rid, plan.plan_id, leg.leg_id), ()))}
                        for leg in plan.legs if isinstance(leg, Leg)]
                rows.append({"plan_id": plan.plan_id, "legs": legs,
                             "all_legs_have_resources": bool(legs) and all(leg["count"] for leg in legs)})
            self.requests[rid] = dict(request_id=rid, original_second=snapshot.requests[rid].submitted_at,
                                      plan_count=len(rows), plans=rows,
                                      has_binding_options=any(p["all_legs_have_resources"] for p in rows),
                                      attempts=0, completed_attempts=0, retained_feasible=0,
                                      removed_on_conflict=0, first_conflict=None)

    def attempt(self, encoding):
        for rid, row in self.requests.items():
            if rid in encoding.choices:
                row["attempts"] += 1

    def completed(self, encoding, trial):
        need_conflict = False
        for rid, row in self.requests.items():
            if rid in encoding.choices:
                row["completed_attempts"] += 1
                if trial.feasible and rid in trial.encoding.choices:
                    row["retained_feasible"] += 1
                if rid not in trial.encoding.choices:
                    row["removed_on_conflict"] += 1
                    need_conflict |= row["first_conflict"] is None
        if need_conflict:
            for repair in trial.repairs:
                row = self.requests.get(repair.request_id)
                if row is not None and row["first_conflict"] is None:
                    row["first_conflict"] = repair.reason
        if trial.failure:
            row = self.requests.get(trial.failure.request_id)
            if row is not None and row["first_conflict"] is None:
                row["first_conflict"] = trial.failure.reason

    def record(self, after, result):
        for rid, row in self.requests.items():
            req = after.requests[rid]
            row["final_state"] = req.status.value
            if req.accepted_at is not None:
                outcome = "accepted"
            elif row["retained_feasible"]:
                outcome = "feasible_trial_not_selected"
            elif row["attempts"] > row["completed_attempts"]:
                outcome = "interrupted_attempt"
            elif row["removed_on_conflict"]:
                outcome = "removed_in_observed_trials"
            elif row["attempts"]:
                outcome = "trial_infeasible"
            elif not row["has_binding_options"]:
                outcome = "no_complete_binding_options"
            else:
                outcome = "not_attempted"
            row["outcome"] = outcome
        started = result.stats.candidates_started
        eligible = (started + self.config.candidate_every - 1) // self.config.candidate_every
        return dict(batch=self.batch, second=after.time, stages=self.stages, operator_calls=self.operator_calls,
                    budget_exhausted=result.timing.timed_out, stop_reason=result.timing.stop_reason,
                    candidates_started=started, candidates_evaluated=result.stats.candidates_completed,
                    sampled_candidates=self.sampled_candidates, candidate_samples_truncated=max(
                        0, eligible - self.config.max_candidates_per_batch),
                    detail_spans=self.detail_spans, detail_spans_truncated=self.detail_spans_truncated,
                    requests_truncated=self.requests_eligible - len(self.requests), requests=list(self.requests.values()))


class ProfileRun:
    def __init__(self, path, config):
        self.path = path / "profiling.jsonl"
        with self.path.open("xb"):
            pass
        self.config = config
        self.bytes_written = 0
        self.batches_written = 0
        self.batches_truncated = 0
        self.first_truncated_batch = None
        self.write_wall_ns = 0

    def begin(self, batch):
        if (batch - 1) % self.config.batch_every:
            return None
        if self.batches_truncated:
            self.batches_truncated += 1
            return None
        return BatchProfile(self.config, batch)

    def write(self, profile, after, result, plan, moved):
        start = perf_counter_ns()
        row = profile.record(after, result)
        failure = plan.route_failure
        cross_quota = None
        if plan.transport and plan.transport.status == "OPTIMAL":
            cross_quota = sum(n for (source, target), n in plan.transport.flow.items() if source != target)
        row["m5"] = dict(status=plan.status, solver_status=plan.transport.status if plan.transport else None,
                         cross_quota=cross_quota,
                         published=moved, reason=plan.reason,
                         first_blocked_source=failure.source if failure else None,
                         matched_at_blocked_source=len(failure.assigned_options) if failure else None)
        payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if self.bytes_written + len(payload) > self.config.max_output_bytes:
            self.batches_truncated += 1
            self.first_truncated_batch = profile.batch
        else:
            with self.path.open("ab") as stream:
                stream.write(payload)
            self.bytes_written += len(payload)
            self.batches_written += 1
        self.write_wall_ns += perf_counter_ns() - start

    def counts(self):
        return dict(bytes_written=self.bytes_written, batches_written=self.batches_written,
                    batches_truncated=self.batches_truncated, first_truncated_batch=self.first_truncated_batch,
                    record_and_write_wall_ns=self.write_wall_ns)
