"""PAPER §5.3.2, D006. ID occurrences count only pending formal tasks."""
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from src.m2.passengers import Leg, Plan, Task, WalkLink, task_id


class EncodingError(ValueError):
    """Malformed layer or an assignment outside the batch's sparse LS rows."""


def frozen(mapping):
    return MappingProxyType(dict(mapping))


def _id_tuple(values, name):
    try:
        ids = tuple(values)
    except TypeError as exc:
        raise EncodingError(f"Expected a sequence of {name}") from exc
    if not all(isinstance(k, str) and k.strip() for k in ids):
        raise EncodingError(f"Invalid {name} in encoding")
    return ids


@dataclass(frozen=True)
class Choice:
    plan_id: str
    resources: tuple[str, ...]  # formal legs only, in plan order

    def __post_init__(self):
        if isinstance(self.resources, str) or not isinstance(self.plan_id, str) or not self.plan_id.strip():
            raise EncodingError("Expected a plan ID and a sequence of resource IDs")
        object.__setattr__(self, "resources", _id_tuple(self.resources, "resource IDs"))


@dataclass(frozen=True)
class Encoding:
    choices: Mapping[str, Choice]
    sequence: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.choices, Mapping) or not all(
            isinstance(k, str) and k.strip() and isinstance(v, Choice) for k, v in self.choices.items()
        ):
            raise EncodingError("Expected request IDs mapped to choices")
        object.__setattr__(self, "choices", frozen(self.choices))
        object.__setattr__(self, "sequence", _id_tuple(self.sequence, "request IDs"))

    def without(self, rid):
        return Encoding({k: v for k, v in self.choices.items() if k != rid},
                        tuple(k for k in self.sequence if k != rid))


def committed_choice(req):
    return Choice(req.plan.plan_id, tuple(resource for step, resource in zip(req.plan.legs, req.bindings)
                                          if isinstance(step, Leg)))


def task_chain(plan: Plan, choice: Choice):
    """Use P1's stable identities, including physical walk events in the service chain."""
    if len(choice.resources) != plan.formal_leg_count:
        raise EncodingError(f"Wrong number of formal bindings for {plan.request_id}")
    resources = iter(choice.resources)
    previous = None
    for step in plan.legs:
        if isinstance(step, WalkLink):
            kinds, resource = ("WALK_START", "WALK_END"), None
        else:
            kinds = ("PICKUP", "DROPOFF") if step.mode == "CAR" else ("FLIGHT",)
            resource = next(resources)
        for kind in kinds:
            tid = task_id(plan.request_id, step.leg_id, kind)
            node = step.destination if kind in ("DROPOFF", "WALK_END") else step.origin
            yield Task(tid, plan.request_id, step.leg_id, kind, resource, node, previous)
            previous = tid


def pending_formal(chain):
    return tuple(t for t in chain if t.resource_id is not None and t.status == "PENDING")


def expand(sequence, pending):
    counts = Counter(sequence)
    expected = {rid: len(tasks) for rid, tasks in pending.items() if tasks}
    if counts != expected:
        raise EncodingError(f"Request occurrence counts {dict(counts)} differ from {expected}")
    cursors = {rid: iter(tasks) for rid, tasks in pending.items()}
    return tuple(next(cursors[rid]) for rid in sequence)
