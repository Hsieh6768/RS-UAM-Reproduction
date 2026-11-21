"""D006, §5.3.3. Task associations and weakly connected decoding groups."""
from collections import defaultdict
from dataclasses import dataclass

from src.m2.passengers import Task
from .decoder import _unchecked


@dataclass(frozen=True)
class TaskEdge:
    source: str
    target: str
    kind: str

    @property
    def temporal(self):
        return self.kind != "flight_capacity"


@dataclass(frozen=True)
class TaskComponent:
    request_ids: tuple[str, ...]
    positions: tuple[int, ...]
    tasks: tuple[Task, ...]

    @property
    def component_id(self):
        """Stable within a batch across changes to global interleaving."""
        return self.request_ids

    @property
    def task_ids(self):
        return tuple(t.task_id for t in self.tasks)


@dataclass(frozen=True)
class TaskGraph:
    edges: tuple[TaskEdge, ...]
    components: tuple[TaskComponent, ...]

    @classmethod
    def build(cls, full_decoder, encoding, *, checkpoint=None):
        """The frozen batch resolves every occurrence before any graph is built."""
        check = checkpoint or _unchecked
        check()
        _, _, tasks = full_decoder._resolve(encoding, check)
        return cls.from_tasks(enumerate(tasks), encoding.choices, check)

    @classmethod
    def from_tasks(cls, indexed_tasks, request_ids, checkpoint):
        """Positions may be noncontiguous when rebuilding a repaired component."""
        indexed = tuple(indexed_tasks)
        neighbors = defaultdict(list)
        edges = []
        def connect(source, target, kind):
            edges.append(TaskEdge(source, target, kind))
            neighbors[source].append(target)
            neighbors[target].append(source)

        last_request, last_car = {}, {}
        flights = defaultdict(list)
        for _, task in indexed:
            checkpoint()
            rid, tid, resource = task.request_id, task.task_id, task.resource_id
            if rid in last_request:
                connect(last_request[rid], tid, "request_order")
            last_request[rid] = tid
            if task.kind == "FLIGHT":
                for peer in flights[resource]:
                    checkpoint()
                    connect(peer, tid, "flight_capacity")
                    connect(tid, peer, "flight_capacity")
                flights[resource].append(tid)
            else:
                # Consecutive stops generate the full transitive vehicle order.
                if resource in last_car:
                    connect(last_car[resource], tid, "vehicle_order")
                last_car[resource] = tid

        membership = {}
        groups = []
        for pos, task in indexed:
            checkpoint()
            tid = task.task_id
            if tid not in membership:
                group_id = len(groups)
                groups.append([])
                membership[tid] = group_id
                stack = [tid]
                while stack:
                    checkpoint()
                    for neighbor in neighbors[stack.pop()]:
                        if neighbor not in membership:
                            membership[neighbor] = group_id
                            stack.append(neighbor)
            groups[membership[tid]].append((pos, task))
        components = []
        for rows in groups:
            positions, tasks = zip(*rows)
            components.append(TaskComponent(tuple(sorted({t.request_id for t in tasks})), positions, tasks))
        # An airborne or walking Type3 may need only automatic continuation.
        for rid in sorted(set(request_ids).difference(last_request)):
            components.append(TaskComponent((rid,), (), ()))
        return cls(tuple(edges), tuple(components))
