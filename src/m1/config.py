"""Approved P2 parameters (D001/D008/D011)."""
from dataclasses import dataclass
from decimal import Decimal

from src.common.timebase import nonnegative_decimal


class PlanningConflict(ValueError):
    """A nominal candidate requires a decision about its P1 representation."""


@dataclass(frozen=True)
class M1Config:
    top_k: int = 5
    max_service_legs: int = 3
    max_transfers: int = 2
    walking_speed_mps: Decimal = Decimal("1.4")
    cache_size: int = 1024
    optional_15min_filter: bool = False

    def __post_init__(self):
        for name, minimum in (("top_k", 1), ("max_service_legs", 1),
                              ("max_transfers", 0), ("cache_size", 1)):
            n = getattr(self, name)
            if type(n) is not int or n < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        speed = nonnegative_decimal(self.walking_speed_mps, "walking speed (m/s)")
        if not speed:
            raise ValueError("Walking speed must be positive")
        object.__setattr__(self, "walking_speed_mps", speed)
        if self.optional_15min_filter is not False:
            raise NotImplementedError("The optional 15-minute filter is unimplemented")

    @classmethod
    def from_config(cls, config):
        m1, req, net = config["m1"], config["requests"], config["network"]
        if net["transfer_time_seconds"] != 0:
            raise ValueError("P2 expects zero transfer time at the same station")
        if m1["optional_15min_filter"]["implemented"] is not False:
            raise NotImplementedError("The optional 15-minute filter is unimplemented")
        return cls(top_k=m1["top_k"], max_service_legs=req["max_service_legs"],
                   max_transfers=req["max_transfers"], walking_speed_mps=net["walking_speed_mps"],
                   optional_15min_filter=m1["optional_15min_filter"]["enabled"],
                   cache_size=m1.get("cache_size", 1024))
