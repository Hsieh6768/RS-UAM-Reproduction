import networkx as nx

from src.m2.passengers import Leg, Plan, Request
from src.network.routing import RoadRouter
from src.simulation.engine import Simulator


def world(points, edges, cars=()):
    """Points use explicit longitudes and latitudes around Manhattan."""
    graph = nx.MultiDiGraph()
    for node, lon, lat in points:
        graph.add_node(node, position=(lon, lat))
    for way, (source, target, duration) in enumerate(edges):
        graph.add_edge(source, target, way_id=way, length_m=10, time_s=duration)
    sim = Simulator(RoadRouter(graph), batch_seconds=1)
    for vid, node in cars:
        sim.add_vehicle(vid, node)
    return sim


def request(sim, rid, origin, destination, *, at=0, size=1, **kwargs):
    duration = sim.router.shortest_time_s(origin, destination)
    req = Request(rid, at, origin, destination, size, duration, **kwargs)
    sim.schedule_arrival(rid, at, lambda: req)
    return Plan("plan-" + rid, rid, (
        Leg("car", "CAR", origin, destination, duration, 0, 420, 999),
    ))
