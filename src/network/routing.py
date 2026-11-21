"""D011 static directed routing for nominal travel and physical edge replay."""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
import math
from typing import Union

import networkx as nx

from src.common.timebase import nonnegative_decimal, seconds
from src.network.road import read_road_graph


RoadID = Union[int, str]


class UnreachableRoad(ValueError):
    """The directed OD cannot be traversed on this graph."""


@dataclass(frozen=True)
class RoadEdge:
    source: RoadID
    target: RoadID
    way_id: RoadID
    length_m: Decimal
    time_s: Decimal
    occurrence: int = 0

    @property
    def identity(self):
        return self.source, self.target, self.way_id, self.occurrence


def _id_order(value):
    if type(value) not in (int, str):
        raise ValueError(f"Road and way IDs must be integers or strings: {value!r}")
    return isinstance(value, str), value


class RoadRouter:
    """An immutable network snapshot with bounded, per-instance OD caches.

    Equal-cost paths retain Dijkstra's first discovery in sorted neighbor order.
    Parallel edges minimize (seconds, way ID, length, occurrence). P0 omits
    NetworkX keys, so each directed way's occurrences follow cost/length order.
    """

    def __init__(self, graph: nx.MultiDiGraph):
        if not isinstance(graph, nx.MultiDiGraph):
            raise ValueError("Expected a directed road multigraph")
        self._graph = nx.DiGraph()
        nodes = sorted(graph, key=_id_order)
        digest = sha256()
        for node in nodes:
            lon, lat = map(float, graph.nodes[node]["position"])
            if not (math.isfinite(lon) and math.isfinite(lat)):
                raise ValueError(f"Invalid position for road node {node!r}")
            self._graph.add_node(node, position=(lon, lat))
            digest.update(json.dumps([node, lon, lat], separators=(",", ":")).encode() + b"\n")

        self._places = 0
        for source in nodes:
            for target in sorted(graph[source], key=_id_order):
                alternatives = []
                for data in graph[source][target].values():
                    duration = seconds(data["time_s"])
                    length = nonnegative_decimal(data["length_m"], "meters")
                    alternatives.append((duration, _id_order(data["way_id"]), length))
                # Order equivalent numeric spellings (e.g. 0.10/0.1) as well,
                # so edge occurrences and hash bytes do not depend on input order.
                alternatives.sort(key=lambda row: (*row, str(row[0]), str(row[2])))
                edges = []
                occurrences = Counter()
                for duration, (_, way), length in alternatives:
                    digest.update(json.dumps([source, target, way, str(length), str(duration)],
                                             separators=(",", ":")).encode() + b"\n")
                    edges.append(RoadEdge(source, target, way, length, duration, occurrences[way]))
                    occurrences[way] += 1
                self._graph.add_edge(source, target, roads=tuple(edges))
                self._places = max(self._places, -edges[0].time_s.as_tuple().exponent)

        # Integer ticks at the finest input precision give exact addition and
        # ordering even when the caller has changed the Decimal context.
        scale = 10 ** self._places
        for _, _, data in self._graph.edges(data=True):
            numerator, denominator = data["roads"][0].time_s.as_integer_ratio()
            data["weight"] = numerator * scale // denominator
        self._version = digest.hexdigest()
        self._distance = lru_cache(maxsize=1024)(self._distance_uncached)
        self._path = lru_cache(maxsize=256)(self._path_uncached)
        self._node_ids = tuple(nodes)
        self._entry_nodes = tuple(node for node in nodes if self._graph.in_degree(node))

    @classmethod
    def from_file(cls, path):
        return cls(read_road_graph(path, parse_float=Decimal))

    @property
    def version(self):
        """SHA256 of the ordered graph content used to construct this router."""
        return self._version

    def position(self, node):
        return self._graph.nodes[node]["position"]

    @property
    def node_ids(self):
        """All immutable road node IDs in routing order."""
        return self._node_ids

    @property
    def entry_nodes(self):
        """Nodes with a physical incoming road edge, in routing order."""
        return self._entry_nodes

    def parallel_edges(self, source_node, target_node) -> tuple:
        """Return every directed physical edge in deterministic routing preference order."""
        data = self._graph.get_edge_data(source_node, target_node)
        return data["roads"] if data else ()

    def shortest_time_s(self, source_node, target_node) -> Decimal:
        """Query only the distance, without constructing or caching a path."""
        return self._distance(source_node, target_node)

    def shortest_path_edges(self, source_node, target_node) -> tuple:
        return self._path(source_node, target_node)

    def shortest_times_from(self, source_node):
        """Exact times from one origin to all reachable nodes, including itself."""
        try:
            costs = nx.single_source_dijkstra_path_length(self._graph, source_node, weight="weight")
        except nx.NodeNotFound as exc:
            raise UnreachableRoad(f"Unknown road source {source_node!r}") from exc
        return {node: self._duration(cost) for node, cost in costs.items()}

    def _duration(self, cost):
        return Decimal((0, Decimal(cost).as_tuple().digits, -self._places))

    def _distance_uncached(self, source, target):
        try:
            cost = nx.dijkstra_path_length(self._graph, source, target, weight="weight")
        except (nx.NodeNotFound, nx.NetworkXNoPath) as exc:
            raise UnreachableRoad(f"No directed road path {source!r} -> {target!r}") from exc
        return self._duration(cost)

    def _path_uncached(self, source, target):
        try:
            nodes = nx.dijkstra_path(self._graph, source, target, weight="weight")
        except (nx.NodeNotFound, nx.NetworkXNoPath) as exc:
            raise UnreachableRoad(f"No directed road path {source!r} -> {target!r}") from exc
        return tuple(self._graph[a][b]["roads"][0] for a, b in zip(nodes, nodes[1:]))
