"""Read the P0 network selected by a resolved project configuration."""
from dataclasses import dataclass
from pathlib import Path

from src.common.geo import mph_to_mps
from src.data.instance import network_references, read_json
from src.network.flights import AirNetwork
from src.network.routing import RoadRouter


@dataclass(frozen=True)
class PreparedNetwork:
    road: RoadRouter
    air: AirNetwork

    @classmethod
    def from_config(cls, config, *, root=Path(".")):
        settings = config["network"]
        manifest, _, roads = network_references(config, Path(root), verify=True)
        road_audit, air_audit = roads.with_name("road_audit.json"), roads.with_name("air_audit.json")
        audit = read_json(road_audit)
        for mode in ("car", "evtol"):
            if mph_to_mps(settings[f"{mode}_speed_mph"]) != audit[f"{mode}_speed_mps"]:
                raise ValueError(f"Configured {mode} speed differs from P0 travel weights")
        road = RoadRouter.from_file(roads)
        selected = manifest.get("dataset_variant") == "scc100"
        air = AirNetwork.from_p0(air_audit, station_field="road_node" if selected else "full_road_node",
                                 phase_s=settings["flight_schedule_phase_seconds"],
                                 capacity=settings["evtol_capacity"], headway_s=settings["flight_headway_seconds"])
        if selected and (len(air.station_nodes), len(air.routes)) != (10, 60):
            raise ValueError("SCC100 requires all 10 vertiports and 60 directed routes")
        for node in air.station_nodes.values():
            road.position(node)
        return cls(road, air)
