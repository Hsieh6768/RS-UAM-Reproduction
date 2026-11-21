"""Small deterministic input for the public simulation entry point."""
from decimal import Decimal
from functools import partial
from types import MappingProxyType

import networkx as nx

from src.m2.passengers import Request
from src.network.flights import AirNetwork, AirRoute
from src.network.prepared import PreparedNetwork
from src.network.routing import RoadRouter


DEMAND = ((0, "warm", 1, 2), (13, "eval", 2, 1), (30, "tail", 1, 2))
COVERAGE_SECONDS = 1200


def micro_network():
    graph = nx.MultiDiGraph()
    graph.add_node(1, position=(-73.999, 40.751))
    graph.add_node(2, position=(-73.989, 40.751))
    for u, v in ((1, 2), (2, 1)):
        graph.add_edge(u, v, way_id=1, length_m=840, time_s=17)
    route = AirRoute("AB", "A", "B", Decimal(840), Decimal(5), 180)
    return PreparedNetwork(RoadRouter(graph), AirNetwork(MappingProxyType({"A": 1, "B": 2}),
                           MappingProxyType({"AB": route}), 0, 4))


def micro_envelopes(net, pickup, delay):
    for at, rid, u, v in DEMAND:
        yield at, rid, partial(Request, rid, at, u, v, 1, net.road.shortest_time_s(u, v), pickup, delay)

