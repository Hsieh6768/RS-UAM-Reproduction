from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from src.common.config import load_config
from src.common.geo import mph_to_mps
from src.data.sources import archive_file
from src.data.tlc import COLUMNS, classify, stratified_sample
from src.network.air import audit_air
from src.network.road import build_graph, map_requests, summarize_graph


@pytest.fixture
def p0_scratch():
    # The Windows test runner denies access to directories created with mode 0700.
    path = Path("outputs") / f"p0-test-{uuid4().hex}"
    path.mkdir()
    yield path
    for child in path.iterdir():
        child.unlink()
    path.rmdir()


def test_archive_verifies_content_and_refuses_collision(p0_scratch):
    tmp_path = p0_scratch
    source = tmp_path / "original"
    target = tmp_path / "archive"
    source.write_bytes(b"raw\r\n")
    first = archive_file(source, target)
    assert source.read_bytes() == target.read_bytes() == b"raw\r\n"
    assert first == archive_file(source, target)
    target.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        archive_file(source, target)
    assert target.read_bytes() == b"other"


def test_config_extends_recursively_and_applies_explicit_override(p0_scratch):
    tmp_path = p0_scratch
    (tmp_path / "base.json").write_text('{"x":{"a":1,"b":2},"z":true}')
    child = tmp_path / "child.json"
    child.write_text('{"extends":"base.json","x":{"b":3}}')
    assert load_config(child, {"x": {"a": 4}}) == {"x": {"a": 4, "b": 3}, "z": True}
    with pytest.raises(FileNotFoundError, match="missing.json"):
        load_config(tmp_path / "missing.json")
    child.write_text('{"extends":"missing.json"}')
    with pytest.raises(FileNotFoundError, match="missing.json"):
        load_config(child)
    child.write_text('{"extends":"child.json"}')
    with pytest.raises(ValueError, match="cycle"):
        load_config(child)


def test_yaml_extends_actual_paper_config():
    config = load_config(Path("configs/paper_main.yaml"))
    assert config["simulation"]["batch_seconds"] == 10
    assert config["network"]["directed_roads"] is True
    assert config["network"]["road_graph_component_policy"] == "full_directed_reachable_od"
    assert config["network"]["manhattan_boundary_relation"] == 8398124
    assert config["inputs"]["prepared_road_graph_path"] == "data/processed/p0_network/roads.json"
    assert config["inputs"]["prepared_request_manifest_path"] == "data/processed/p0_requests/manifest.json"
    assert config["reporting"]["console_every_batch"] is True


def test_air_audit_preserves_asymmetric_edges_and_computes_distances():
    stations = [
        {"vertiport_id": "A", "longitude": 0, "latitude": 0, "transfer_time_seconds": 0},
        {"vertiport_id": "B", "longitude": 0.01, "latitude": 0, "transfer_time_seconds": 0},
        {"vertiport_id": "C", "longitude": 0.02, "latitude": 0, "transfer_time_seconds": 0},
    ]
    routes = [
        {"route_id": "1", "origin_id": "A", "destination_id": "B", "headway_seconds": 180},
        {"route_id": "2", "origin_id": "B", "destination_id": "C", "headway_seconds": 180},
    ]
    result = audit_air(stations, routes, speed_mps=75.9968, max_distance_m=193121.28)
    assert result["unpaired_directed_edges"] == 2
    assert result["routes"][0]["distance_m"] == pytest.approx(1111.95, abs=0.02)
    assert [(r["origin_id"], r["destination_id"]) for r in result["routes"]] == [
        ("A", "B"), ("B", "C")
    ]
    assert result["routes"][0]["nominal_time_s"] == pytest.approx(14.63, abs=0.01)
    with pytest.raises(ValueError, match="unknown endpoint"):
        audit_air(stations[:2], routes, 75.9968, 193121.28)


def test_cleaning_precedence_and_history_flags():
    columns = dict(zip(COLUMNS, [
        "2013-05-05T00:00:00.000", "2013-05-04T00:00:00.000",
        "-74", "40.7", "-73.99", "40.7", "2", "0",
    ]))
    frame = pd.DataFrame([
        columns,
        {**columns, "pickup_latitude": "0", "passenger_count": "6"},
        {**columns, "dropoff_longitude": "-73.9999", "passenger_count": "0"},
        {**columns, "dropoff_longitude": "-73.9999"},
        {**columns, "tpep_pickup_datetime": "2013-05-04T21:59:59.000"},
        {**columns, "tpep_pickup_datetime": "2013-05-07T00:00:00.000"},
        {**columns, "dropoff_longitude": "-73", "passenger_count": "8"},
        {**columns, "tpep_pickup_datetime": "nonsense"},
        {**columns, "tpep_pickup_datetime": "2013-05-04T22:00:00.000"},
    ])
    eligible, flags, reasons = classify(
        frame, box(-74.1, 40.6, -73.9, 40.9), 4, 500,
        ("2013-05-04T22:00:00", "2013-05-05T00:00:00", "2013-05-07T00:00:00"),
    )
    assert reasons.tolist() == [
        "", "invalid_coordinates", "invalid_party_size", "short_direct_od",
        "before_warmup", "", "outside_manhattan", "invalid_pickup_time", "",
    ]
    assert eligible["cohort"].tolist() == ["EVALUATION", "TAIL", "WARMUP"]
    assert flags.loc[1, "zero_coordinate"] and flags.loc[1, "party_over_max"]
    assert flags.loc[2, "short_direct_od"] and flags.loc[2, "party_zero"]
    assert flags.loc[0, "historical_duration_nonpositive"] and flags.loc[0, "historical_miles_nonpositive"]


def test_hourly_sampling_is_reproducible_and_retains_timestamp_and_unique_rows():
    hours = ["2013-05-05T00", "2013-05-05T01", "2013-05-06T00"]
    times = [hour + ":00:" + second for hour in hours for second in ["00"] * 10 + ["07"] * 20]
    rows = pd.DataFrame({
        "request_id": np.arange(1, 91),
        "tpep_pickup_datetime": times,
        "hour": np.repeat(hours, 30),
        "cohort": "EVALUATION",
    })
    sampled, quotas = stratified_sample(rows, 0.05, 20130505)
    assert quotas["sampled"].tolist() == [2, 1, 1]
    assert sampled["request_id"].is_unique
    pd.testing.assert_frame_equal(sampled, stratified_sample(rows.iloc[::-1], 0.05, 20130505)[0])
    assert sampled["tpep_pickup_datetime"].tolist() == [
        times[i - 1] for i in sampled["request_id"]
    ]


def test_directed_components_and_snap_comparison():
    graph = build_graph(
        {1: (-74, 40.7), 2: (-73.99, 40.7), 3: (-73.98, 40.7), 4: (-73.97, 40.7)},
        [
            (10, [1, 2], {"highway": "primary"}),
            (11, [2, 3], {"highway": "secondary", "oneway": "yes"}),
            (12, [4, 3], {"highway": "secondary", "oneway": "-1"}),
        ],
        mph_to_mps(14.9),
    )
    audit, components, largest = summarize_graph(graph)
    assert graph.number_of_edges() == 4
    assert not graph.has_edge(3, 2)
    assert graph.has_edge(3, 4) and not graph.has_edge(4, 3)
    assert graph[1][2][10]["length_m"] == pytest.approx(843, abs=2)
    assert graph[1][2][10]["time_s"] == pytest.approx(126.6, abs=0.5)
    assert audit["strong_components"] == 3 and audit["weak_components"] == 1
    assert largest == {1, 2} and audit["largest_strong_node_fraction"] == 0.5
    mapped, impacts = map_requests(
        graph, components, largest,
        np.array([[-74, 40.7], [-73.97, 40.7]]),
        np.array([[-73.97, 40.7], [-74, 40.7]]),
    )
    assert mapped["road_reachable"].tolist() == [True, False]
    assert mapped["origin_node"].tolist() == [1, 4]
    assert impacts["either_endpoint_changed"] == 2
    assert impacts["max_largest_snap_m"] > 1600


def test_paper_speed_conversions():
    assert mph_to_mps(14.9) == pytest.approx(6.660896)
    assert mph_to_mps(170) == pytest.approx(75.9968)
