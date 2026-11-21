"""Fixed metric cells for Manhattan and spherical center costs (D010)."""
from collections import defaultdict
from dataclasses import dataclass
from math import cos, degrees, floor, isfinite, radians
from types import MappingProxyType
from typing import Mapping, Optional

from src.common.geo import EARTH_RADIUS_M, distance_m
from src.network.routing import RoadID, RoadRouter

Cell = tuple[int, int]
ORIGIN_LON_LAT = (-74.0, 40.75)
_EAST_RADIUS_M = EARTH_RADIUS_M * cos(radians(ORIGIN_LON_LAT[1]))


def project_m(lon_lat):
    """Local equidistant cylindrical projection with true scale at 40.75°N."""
    lon, lat = lon_lat
    return (_EAST_RADIUS_M * radians(lon - ORIGIN_LON_LAT[0]),
            EARTH_RADIUS_M * radians(lat - ORIGIN_LON_LAT[1]))


def unproject_m(xy):
    x, y = xy
    return (ORIGIN_LON_LAT[0] + degrees(x / _EAST_RADIUS_M),
            ORIGIN_LON_LAT[1] + degrees(y / EARTH_RADIUS_M))


def cell_at_m(xy, size_m):
    """West and south boundaries are included, east and north excluded."""
    x, y = xy
    return floor(x / size_m), floor(y / size_m)


def check_size(size_m):
    if type(size_m) not in (int, float) or not isfinite(size_m) or size_m <= 0:
        raise ValueError("m5.grid_size_m must be a positive finite length in metres")


def _center_m(cell, size):
    column, row = cell
    return (column + .5) * size, (row + .5) * size


@dataclass(frozen=True)
class RoadGrid:
    graph_version: str
    size_m: float
    bounds: Optional[tuple[int, int, int, int]]
    node_cells: Mapping[RoadID, Cell]
    entries: Mapping[Cell, tuple[RoadID, ...]]

    @classmethod
    def build(cls, road: RoadRouter, size_m=1000):
        check_size(size_m)
        points = {node: project_m(road.position(node)) for node in road.node_ids}
        cells = {node: cell_at_m(point, size_m) for node, point in points.items()}
        bounds = None
        if cells:
            cols, rows = zip(*cells.values())
            bounds = min(cols), min(rows), max(cols), max(rows)

        by_cell = defaultdict(list)
        for node in road.entry_nodes:
            by_cell[cells[node]].append(node)

        def center_distance_sq(node, center):
            return sum((a - b)**2 for a, b in zip(points[node], center))

        entries = {}
        for cell, nodes in sorted(by_cell.items()):
            center = _center_m(cell, size_m)
            # Input order is the router's stable road node ID order.
            entries[cell] = tuple(sorted(nodes, key=lambda n: center_distance_sq(n, center)))
        return cls(road.version, size_m, bounds, MappingProxyType(cells),
                   MappingProxyType(entries))

    @property
    def cell_count(self):
        """Count all cells in the covered rectangle, including ones with no road."""
        if self.bounds is None:
            return 0
        x0, y0, x1, y1 = self.bounds
        return (x1 - x0 + 1) * (y1 - y0 + 1)

    def center(self, cell: Cell):
        return unproject_m(_center_m(cell, self.size_m))

    def cost_m(self, source: Cell, target: Cell):
        if source == target:
            return 0.
        return float(distance_m([self.center(source)], [self.center(target)])[0])
