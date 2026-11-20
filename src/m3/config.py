"""Approved D009 resource screening parameters."""
from dataclasses import dataclass

from src.common.geo import mph_to_mps
from src.common.timebase import nonnegative_decimal


@dataclass(frozen=True)
class M3Config:
    vehicle_speed_mph: float = 14.9
    vehicle_capacity: int = 4
    candidate_limit_multiplier: int = 2

    def __post_init__(self):
        speed = float(nonnegative_decimal(self.vehicle_speed_mph, "vehicle speed in mph"))
        if not 0 < speed < float("inf"):
            raise ValueError("Vehicle speed must be positive and finite")
        object.__setattr__(self, "vehicle_speed_mph", speed)
        for field in ("vehicle_capacity", "candidate_limit_multiplier"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer")

    @property
    def vehicle_speed_mps(self):
        return mph_to_mps(self.vehicle_speed_mph)

    @property
    def candidate_limit(self):
        return self.vehicle_capacity * self.candidate_limit_multiplier

    @classmethod
    def from_config(cls, config):
        m3, network = config["m3"], config["network"]
        if (m3["spatial_query"] != "rtree_bounding_square" or m3["distance_sort"] != "straight_line"
                or m3["include_currently_full_vehicles"] is not True
                or m3["committed_type3_bypass_normal_truncation"] is not True):
            raise ValueError("Configuration conflicts with the D009 screening rules")
        return cls(network["car_speed_mph"], network["car_capacity"], m3["candidate_limit_multiplier"])
