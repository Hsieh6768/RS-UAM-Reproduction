"""Small directed P1 networks with explicit metric positions and travel seconds."""
from decimal import Decimal

import networkx as nx

from src.m2.passengers import Request
from src.network.flights import AirNetwork, AirRoute
from src.network.prepared import PreparedNetwork
from src.network.routing import RoadRouter


def network(positions, roads, stations=(), flights=()):
    graph = nx.MultiDiGraph()
    for i, lon in enumerate(positions):
        graph.add_node(i, position=(lon, 0))
    for i, (u, v, duration) in enumerate(roads):
        graph.add_edge(u, v, way_id=i, length_m=1, time_s=duration)
    mapping = {f"S{n}": n for n in stations}
    routes = {}
    for u, v, duration in flights:
        key = f"{u}>{v}"
        routes[key] = AirRoute(key, f"S{u}", f"S{v}", Decimal(1000), Decimal(duration), 180)
    return PreparedNetwork(RoadRouter(graph), AirNetwork(mapping, routes, 0, 4))


def corridor():
    return network([0, .01, .02, .03, .04], [(0, 1, 10), (1, 2, 20),
                   (2, 3, 30), (2, 1, 5)], (1, 2, 4), [(1, 2, 5)])


def request(net, dest=3, **kwargs):
    return Request("R", 0, 0, dest, 1, net.road.shortest_time_s(0, dest), **kwargs)
