import json
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def audit():
    return {
        "stations_count": 2,
        "directed_edges": 1,
        "stations": [
            {"vertiport_id": "B", "full_road_node": 29, "largest_road_node": 99,
             "transfer_time_seconds": 0},
            {"vertiport_id": "A", "full_road_node": 13, "largest_road_node": 98,
             "transfer_time_seconds": 0},
        ],
        "routes": [
            {"route_id": "east", "origin_id": "A", "destination_id": "B",
             "headway_seconds": 180, "distance_m": 787.5, "nominal_time_s": 10.25,
             "within_distance_limit": True},
        ],
    }


@pytest.fixture
def load_air():
    from src.network.flights import AirNetwork

    path = Path("outputs") / f"p1-air-test-{uuid4().hex}.json"

    def load(audit, **settings):
        path.write_text(json.dumps(audit), encoding="utf-8")
        return AirNetwork.from_p0(path, **settings)

    yield load
    path.unlink(missing_ok=True)


def test_directed_routes_and_full_graph_station_mapping(audit, load_air):
    net = load_air(audit)
    assert dict(net.station_nodes) == {"A": 13, "B": 29}
    assert [(r.origin_id, r.destination_id) for r in net.routes.values()] == [("A", "B")]
    route = net.routes["east"]
    assert route.nominal_time_s == Decimal("10.25")
    assert route.distance_m == Decimal("787.5")
    with pytest.raises(TypeError):
        net.station_nodes["A"] = 98


def test_fixed_flights_use_phase_headway_and_ceil_arrival(audit, load_air):
    net = load_air(audit, phase_s=17, capacity=3, headway_s=180)
    first = net.flight("east", 0)
    later = net.flight("east", 2)
    assert (first.origin, first.destination, first.route_id) == (13, 29, "east")
    assert (first.depart_at, first.arrive_at, first.capacity) == (17, 28, 3)
    assert (later.depart_at, later.arrive_at) == (377, 388)
    assert first.flight_id != later.flight_id
    assert later == load_air(audit, phase_s=17, capacity=3).flight("east", 2)


@pytest.mark.parametrize("key,value", [
    ("destination_id", "C"), ("destination_id", "A"), ("within_distance_limit", False),
    ("nominal_time_s", -1), ("nominal_time_s", float("nan")), ("distance_m", 0),
    ("headway_seconds", 1.5),
])
def test_unusable_route_fails_during_load(audit, load_air, key, value):
    audit["routes"][0][key] = value
    with pytest.raises(ValueError):
        load_air(audit)


def test_partial_or_duplicate_audit_is_rejected(audit, load_air):
    audit["directed_edges"] = 2
    with pytest.raises(ValueError):
        load_air(audit)
    audit["routes"].append(dict(audit["routes"][0], route_id="same-pair"))
    with pytest.raises(ValueError):
        load_air(audit)


@pytest.mark.parametrize("settings", [
    {"phase_s": -1}, {"phase_s": 3.5}, {"capacity": 0}, {"capacity": True}, {"headway_s": 60},
])
def test_invalid_or_inconsistent_schedule_fails(audit, load_air, settings):
    with pytest.raises(ValueError):
        load_air(audit, **settings)


def test_only_defined_flights_are_addressable(audit, load_air):
    net = load_air(audit)
    for index in (-1, 1.5, False):
        with pytest.raises(ValueError):
            net.flight("east", index)
    with pytest.raises(KeyError):
        net.flight("west", 0)


def test_long_schedule_uses_exact_integer_epoch_translation(audit, load_air):
    net = load_air(audit, phase_s=17)
    with localcontext() as context:
        context.prec = 3
        # 17 + 2 * 180 + 10.25 = 387.25, whose event is at second 388.
        assert net.flight("east", 2).arrive_at == 388
