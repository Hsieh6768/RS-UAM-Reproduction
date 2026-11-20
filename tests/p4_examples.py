from src.m2.passengers import Leg, Plan, Request
from src.m3 import M3Searcher
from src.m4 import Choice, Encoding, FullDecoder
from src.simulation.engine import Simulator
from tests.p2_examples import network


def setup(roads, cars=(), flights=()):
    nodes = max(max(a, b) for a, b, _ in roads) + 1
    road = network([i * .00001 for i in range(nodes)], roads).road
    sim = Simulator(road, batch_seconds=1)
    for vid, node, capacity in cars:
        sim.add_vehicle(vid, node, capacity)
    for flight in flights:
        sim.add_flight(flight)
    return sim


def leg(name, a, b, duration, mode="CAR", ep=0, lp=420, ld=999):
    return Leg(name, mode, a, b, duration, ep, lp, ld)


def release(sim, rid, steps, size=1, **kw):
    plan = Plan("p-" + rid, rid, tuple(steps))
    a, b = steps[0].origin, steps[-1].destination
    req = Request(rid, 0, a, b, size, sim.router.shortest_time_s(a, b), **kw)
    sim.schedule_arrival(rid, 0, lambda: req)
    return plan


def batch(sim, *plans, **kwargs):
    snapshot = sim.batch_snapshot()
    options = {p.request_id: (p,) for p in plans}
    sparse = M3Searcher(sim.router).search(snapshot, options)
    return FullDecoder(sim.router, snapshot, options, sparse, **kwargs)


def encoding(bindings, sequence):
    return Encoding({rid: Choice("p-" + rid, resources) for rid, resources in bindings.items()},
                    tuple(sequence))
