import json
from pathlib import Path
from uuid import uuid4

import pytest

from src.data.sources import fingerprint
from src.network.prepared import PreparedNetwork


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def inputs():
    # P0 and P1 use process-owned workspace files on Windows.
    root = Path("outputs") / ("p1-inputs-" + uuid4().hex)
    root.mkdir()
    try:
        road = write(root / "roads.json", {"nodes": [[1, 0, 0], [2, 1, 0]],
                     "directed_edges": [[1, 2, 33, 13.321792, 2]]})
        air = write(root / "air_audit.json", {
            "stations_count": 2, "directed_edges": 1,
            "stations": [{"vertiport_id": str(n), "full_road_node": n, "transfer_time_seconds": 0}
                         for n in (1, 2)],
            "routes": [{"route_id": "f", "origin_id": "1", "destination_id": "2",
                        "within_distance_limit": True, "distance_m": 75.9968,
                        "headway_seconds": 180, "nominal_time_s": 1}]})
        audit = write(root / "road_audit.json", {"car_speed_mps": 6.660896, "evtol_speed_mps": 75.9968})
        network = write(root / "manifest.json", {"artifacts": {
            p.name: fingerprint(p) for p in (road, air, audit)}})
        write(root / "requests.json", {"status": "OFFICIAL_P0_INPUTS",
              "official_road_network": {"path": road.name, "directed": True,
              "policy": "full_directed_reachable_od", **fingerprint(road)},
              "network_manifest": fingerprint(network)})
        config = {"inputs": {"prepared_road_graph_path": road.name,
                             "prepared_request_manifest_path": "requests.json"},
                  "network": {"directed_roads": True, "road_graph_component_policy": "full_directed_reachable_od",
                              "car_speed_mph": 14.9, "evtol_speed_mph": 170, "evtol_capacity": 4,
                              "flight_headway_seconds": 180, "flight_schedule_phase_seconds": 0}}
        yield root, config
    finally:
        for path in root.iterdir():
            path.unlink()
        root.rmdir()


def test_configured_inputs_use_p0_weights_and_full_station_nodes(inputs):
    root, cfg = inputs
    net = PreparedNetwork.from_config(cfg, root=root)
    assert net.road.shortest_time_s(1, 2) == 2
    assert net.air.flight("f", 2).arrive_at == 361
    assert net.air.station_nodes["1"] == 1


def test_configured_speed_must_match_audited_weights(inputs):
    root, cfg = inputs
    cfg["network"]["car_speed_mph"] = 15
    with pytest.raises(ValueError, match="speed"):
        PreparedNetwork.from_config(cfg, root=root)


def test_replaced_road_bytes_fail_before_routing(inputs):
    root, cfg = inputs
    write(root / "roads.json", {})
    with pytest.raises(ValueError, match="roads.json"):
        PreparedNetwork.from_config(cfg, root=root)
