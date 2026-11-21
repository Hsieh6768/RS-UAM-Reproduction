"""M4-only cooperative deadlines and per-batch timing statistics (D014)."""
from dataclasses import dataclass
from math import floor, isfinite
from statistics import mean
from time import perf_counter, process_time
from typing import Optional


class BudgetExpired(Exception):
    """Normal completion of the search at a cooperative checkpoint."""


@dataclass(frozen=True)
class BudgetTiming:
    wall_s: float
    cpu_s: float
    stop_reason: Optional[str]
    wall_overrun_s: float
    cpu_overrun_s: float

    @property
    def timed_out(self):
        return self.stop_reason is not None


class SearchBudget:
    """Start at solve entry. Both clocks measure elapsed time up to finish()."""

    def __init__(self, *, wall_budget_seconds=10., budget_clock='wall',
                 cpu_budget_seconds=None, diagnostic_wall_safety_cap_seconds=None,
                 wall_clock=perf_counter, cpu_clock=process_time, wall_deadline=None):
        self._wall_clock = wall_clock
        self._cpu_clock = cpu_clock
        self._wall_start = wall_clock()
        self._cpu_start = cpu_clock()
        if budget_clock not in ('wall', 'cpu'):
            raise ValueError("Unknown M4 budget clock")
        self._wall_reason = 'wall_deadline'
        self._cpu_limit = None
        wall_limit = wall_budget_seconds
        if budget_clock == 'cpu':
            if cpu_budget_seconds is None or not isfinite(cpu_budget_seconds) or cpu_budget_seconds <= 0:
                raise ValueError("CPU diagnostic requires a positive finite CPU limit")
            self._cpu_limit = cpu_budget_seconds
            wall_limit = diagnostic_wall_safety_cap_seconds
            self._wall_reason = 'wall_safety_cap'
        if wall_limit is None or not isfinite(wall_limit) or wall_limit <= 0:
            raise ValueError("A positive finite wall budget or diagnostic safety cap is required")
        self._wall_end = self._wall_start + wall_limit
        if wall_deadline is not None:
            if not isfinite(wall_deadline):
                raise ValueError("Absolute wall deadline must be finite")
            if wall_deadline < self._wall_end:
                self._wall_reason = 'wall_deadline'
                self._wall_end = wall_deadline
        self._reason = None

    def _expiry(self, wall, cpu_elapsed):
        if wall >= self._wall_end:
            return self._wall_reason
        if self._cpu_limit is not None and cpu_elapsed >= self._cpu_limit:
            return 'cpu_deadline'
        return None

    def checkpoint(self):
        # On the formal wall path, reading the CPU clock only at entry/exit suffices.
        cpu = 0. if self._cpu_limit is None else self._cpu_clock() - self._cpu_start
        self._reason = self._reason or self._expiry(self._wall_clock(), cpu)
        if self._reason is not None:
            raise BudgetExpired(self._reason)

    def finish(self):
        cpu = self._cpu_clock() - self._cpu_start
        wall = self._wall_clock()
        self._reason = self._reason or self._expiry(wall, cpu)
        return BudgetTiming(
            wall - self._wall_start, cpu, self._reason, max(0., wall - self._wall_end),
            0. if self._cpu_limit is None else max(0., cpu - self._cpu_limit),
        )


def _distribution(values):
    ordered = sorted(values)
    if not ordered:
        return dict.fromkeys(('mean', 'p95', 'p99', 'max'))

    def quantile(fraction):
        # Linear interpolation between adjacent order statistics (type 7).
        pos = fraction * (len(ordered) - 1)
        left = floor(pos)
        right = min(left + 1, len(ordered) - 1)
        return ordered[left] + (pos - left) * (ordered[right] - ordered[left])

    return dict(mean=mean(ordered), p95=quantile(.95), p99=quantile(.99), max=ordered[-1])


def aggregate_timings(records):
    """Summarize completed M4 calls, including cooperative deadline overshoot."""
    records = tuple(records)
    timeouts = sum(record.timed_out for record in records)
    return {
        'batches': len(records),
        'timeouts': timeouts,
        'timeout_rate': timeouts / len(records) if records else None,
        'wall_s': _distribution(record.wall_s for record in records),
        'cpu_s': _distribution(record.cpu_s for record in records),
        'max_wall_overrun_s': max((r.wall_overrun_s for r in records), default=None),
        'max_cpu_overrun_s': max((r.cpu_overrun_s for r in records), default=None),
    }
