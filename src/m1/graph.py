"""PAPER §3.1/§5.1. The vertices of the two layers have distinct identities."""
from dataclasses import dataclass
from decimal import Decimal
from heapq import heappop, heappush
from itertools import count

from src.common.geo import distance_m
from src.common.timebase import exact_add, seconds
from src.m1.config import PlanningConflict
from src.m2.passengers import WalkLink
from src.network.routing import UnreachableRoad


def id_order(node):
    return isinstance(node, str), node


@dataclass(frozen=True)
class Edge:
    source: tuple
    target: tuple
    kind: str
    origin: object
    destination: object
    duration: Decimal
    route_id: str = ""
    metres: Decimal = Decimal(0)

    @property
    def formal(self):
        return self.kind in ("CAR", "FLIGHT")

    @property
    def signature(self):
        return self.kind, id_order(self.origin), id_order(self.destination), self.route_id


class TransferNetwork:
    """Fixed station closure for one road/air version and walking speed."""

    def __init__(self, net, config):
        self.road = net.road
        self.speed = config.walking_speed_mps
        self.stations = frozenset(net.air.station_nodes.values())
        if len(self.stations) != len(net.air.station_nodes):
            raise PlanningConflict("Distinct stations share a road node; interstation access needs a distance")
        self.fixed_roads = {}
        self.air_edges = []
        for station, node in sorted(net.air.station_nodes.items()):
            ground, air = ("ground", node), ("air", station)
            for u, v in ((ground, air), (air, ground)):
                self.air_edges.append(Edge(u, v, "TRANSFER", node, node, Decimal(0)))
        for key, route in sorted(net.air.routes.items()):
            u, v = route.origin_id, route.destination_id
            self.air_edges.append(Edge(("air", u), ("air", v), "FLIGHT", net.air.station_nodes[u],
                                       net.air.station_nodes[v], seconds(route.nominal_time_s), key))

    def ground_edge(self, u, v, direct):
        # Direct OD retains the original request's P0 eligibility after snapping.
        key = u, v, direct
        fixed = u in self.stations and v in self.stations
        if fixed and key in self.fixed_roads:
            return self.fixed_roads[key]
        try:
            duration = self.road.shortest_time_s(u, v)
        except UnreachableRoad:
            edge = None
        else:
            src, dst = ("ground", u), ("ground", v)
            edge = Edge(src, dst, "CAR", u, v, duration)
            if not direct:
                metres = Decimal(str(distance_m(self.road.position(u), self.road.position(v))[0]))
                if metres < 500:
                    if not metres:
                        raise PlanningConflict(f"Distinct road nodes {u!r}, {v!r} have identical coordinates")
                    duration = WalkLink("walk", u, v, metres, self.speed).nominal_s
                    edge = Edge(src, dst, "WALK", u, v, duration, metres=metres)
        if fixed:
            self.fixed_roads[key] = edge
        return edge

    def for_request(self, req):
        nodes = sorted(self.stations | {req.origin, req.destination}, key=id_order)
        edges = list(self.air_edges)
        for u in nodes:
            for v in nodes:
                if u != v:
                    direct = (u, v) == (req.origin, req.destination)
                    edge = self.ground_edge(u, v, direct)
                    if edge is not None:
                        if direct and edge.duration != req.direct_road_time_s:
                            raise ValueError(f"{req.request_id}: direct road time differs from the fixed network")
                        edges.append(edge)
        return RequestGraph(req, edges)


class RequestGraph:
    def __init__(self, req, edges):
        self.origin = ("ground", req.origin)
        self.destination = ("ground", req.destination)
        self.edges = tuple(edges)
        self.incoming, self.outgoing = {}, {}
        for edge in edges:
            self.incoming.setdefault(edge.target, []).append(edge)
            self.outgoing.setdefault(edge.source, []).append(edge)


def build_graph(request, network, config):
    return TransferNetwork(network, config).for_request(request)


def earliest_bounds(graph, submitted_at):
    """Eq. (20). Dijkstra on abstract trip edges only; missing means unreachable."""
    start = seconds(submitted_at)
    bounds = {graph.origin: start}
    seq = count()
    heap = [(start, next(seq), graph.origin)]
    while heap:
        arrival, _, node = heappop(heap)
        if arrival != bounds[node]:
            continue
        for edge in graph.outgoing.get(node, ()):
            new = exact_add(arrival, edge.duration)
            if edge.target not in bounds or new < bounds[edge.target]:
                bounds[edge.target] = new
                heappush(heap, (new, next(seq), edge.target))
    return bounds
