"""Read the P0 network selected by a resolved project configuration."""
from dataclasses import dataclass
from pathlib import Path

from src.common.geo import mph_to_mps
from src.data.p0 import read_json
from src.data.sources import fingerprint
from src.network.flights import AirNetwork
from src.network.routing import RoadRouter


def _verify(path, expected):
    observed = fingerprint(path)
    if any(expected[k] != value for k, value in observed.items()):
        raise ValueError(f"P0 fingerprint mismatch for {path}")


@dataclass(frozen=True)
class PreparedNetwork:
    road: RoadRouter
    air: AirNetwork

    @classmethod
    def from_config(cls, config, *, root=Path(".")):
        inputs, settings = config["inputs"], config["network"]
        root = Path(root)
        manifest = read_json(root / inputs["prepared_request_manifest_path"])
        road_ref = manifest["official_road_network"]
        if (manifest["status"] != "OFFICIAL_P0_INPUTS" or road_ref["directed"] is not True
                or settings["directed_roads"] is not True or road_ref["policy"] != "full_directed_reachable_od"
                or settings["road_graph_component_policy"] != road_ref["policy"]):
            raise ValueError("Expected the approved full directed P0 network")
        roads = root / inputs["prepared_road_graph_path"]
        if roads.resolve() != (root / road_ref["path"]).resolve():
            raise ValueError("Configured road path differs from the approved P0 manifest")
        _verify(roads, road_ref)
        network_manifest = roads.with_name("manifest.json")
        _verify(network_manifest, manifest["network_manifest"])
        artifacts = read_json(network_manifest)["artifacts"]
        if artifacts[roads.name]["sha256"] != road_ref["sha256"]:
            raise ValueError("Road SHA256 differs between P0 manifests")
        road_audit, air_audit = roads.with_name("road_audit.json"), roads.with_name("air_audit.json")
        for path in (road_audit, air_audit):
            _verify(path, artifacts[path.name])
        audit = read_json(road_audit)
        for mode in ("car", "evtol"):
            if mph_to_mps(settings[f"{mode}_speed_mph"]) != audit[f"{mode}_speed_mps"]:
                raise ValueError(f"Configured {mode} speed differs from P0 travel weights")
        road = RoadRouter.from_file(roads)
        air = AirNetwork.from_p0(air_audit, phase_s=settings["flight_schedule_phase_seconds"],
                                 capacity=settings["evtol_capacity"], headway_s=settings["flight_headway_seconds"])
        for node in air.station_nodes.values():
            road.position(node)
        return cls(road, air)
