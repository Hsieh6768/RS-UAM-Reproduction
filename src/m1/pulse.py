"""Reverse simple-path Pulse, PAPER §5.1 / Algorithm 2, with D008 Top-K."""
from decimal import Decimal, localcontext

from src.common.timebase import exact_add
from src.m1.graph import earliest_bounds
from src.m2.passengers import Leg, Plan, WalkLink


class TopK:
    """Only K distinct complete structures are retained, ordered by (cost, signature)."""

    def __init__(self, k):
        self.k = k
        self.entries = {}

    @property
    def worst_cost(self):
        if len(self.entries) == self.k:
            return max(rank[0] for rank, value in self.entries.values())
        return None

    def offer(self, cost, signature, value):
        rank = cost, signature
        previous = self.entries.get(signature)
        if previous is not None and previous[0] <= rank:
            return
        if previous is None and len(self.entries) == self.k:
            worst = max(self.entries, key=lambda key: self.entries[key][0])
            if rank >= self.entries[worst][0]:
                return
            del self.entries[worst]
        self.entries[signature] = rank, value

    def values(self):
        return tuple(value for rank, value in sorted(self.entries.values(), key=lambda entry: entry[0]))


def reverse_pulse(req, graph, cfg):
    bounds = earliest_bounds(graph, req.submitted_at)
    top = TopK(cfg.top_k)
    visited = {graph.destination}
    reverse_edges = []
    deadline = req.final_deadline

    def search(node, suffix, formal):
        if node not in bounds:
            return
        arrival = exact_add(bounds[node], suffix)
        if arrival > deadline:
            return
        if formal > cfg.max_service_legs or max(0, formal - 1) > cfg.max_transfers:
            return
        worst = top.worst_cost
        if worst is not None and arrival > exact_add(req.submitted_at, worst):
            return
        if node == graph.origin:
            if not formal:
                return
            steps = tuple(e for e in reversed(reverse_edges) if e.kind != "TRANSFER")
            first_boarding = Decimal(req.submitted_at)
            for step in steps:
                if step.formal:
                    if first_boarding > req.pickup_deadline:
                        return
                    break
                first_boarding = exact_add(first_boarding, step.duration)
            top.offer(suffix, tuple(e.signature for e in steps), steps)
            return
        for edge in graph.incoming.get(node, ()):
            if edge.source in visited:
                continue
            visited.add(edge.source)
            reverse_edges.append(edge)
            search(edge.source, exact_add(suffix, edge.duration), formal + int(edge.formal))
            reverse_edges.pop()
            visited.remove(edge.source)

    search(graph.destination, Decimal(0), 0)
    return top.values()


def subtract_duration(end, duration):
    # D002. Reserve all digits at the common decimal scale even for recurring walks.
    with localcontext() as ctx:
        ctx.prec = max(end.adjusted(), duration.adjusted(), 0) - min(
            end.as_tuple().exponent, duration.as_tuple().exponent, 0) + 2
        return end - duration


def materialize(req, edges, speed, rank):
    latest_end = req.final_deadline
    dropoffs = []
    for e in reversed(edges):
        dropoffs.append(latest_end)
        latest_end = subtract_duration(latest_end, e.duration)

    ready = Decimal(req.submitted_at)
    steps = []
    first = True
    for i, (edge, ld) in enumerate(zip(edges, reversed(dropoffs))):
        leg_id = f"{rank}.{i + 1}"
        if edge.kind == "WALK":
            step = WalkLink(leg_id, edge.origin, edge.destination, edge.metres, speed)
        else:
            lp = Decimal(req.pickup_deadline) if first else subtract_duration(ld, edge.duration)
            step = Leg(leg_id, edge.kind, edge.origin, edge.destination, edge.duration,
                       ready, lp, ld, edge.route_id or None)
            first = False
        steps.append(step)
        ready = exact_add(ready, edge.duration)
    return Plan(f"{req.request_id}/M1/{rank}", req.request_id, tuple(steps))
