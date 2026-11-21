from math import atan2, cos, radians, sin, sqrt

import pytest

from tests.p5_examples import world


def test_p5_metric_grid_and_half_open_boundaries():
    from src.m5.grid import cell_at_m, project_m, unproject_m

    assert project_m((-74, 40.75)) == (0, 0)
    x, y = project_m((-73.99, 40.76))
    assert x == pytest.approx(842.36, abs=0.1)
    assert y == pytest.approx(1111.951, abs=0.01)
    assert unproject_m((x, y)) == pytest.approx((-73.99, 40.76), abs=1e-12)
    for x, y, expected in ((-0.001, 0, (-1, 0)), (0, 0, (0, 0)),
                           (999.999, 1000, (0, 1)), (1000, -1000, (1, -1)),
                           (1000, 2000, (1, 2))):
        assert cell_at_m((x, y), 1000) == expected


def test_p5_grid_includes_cells_without_road_entries_and_uses_spherical_cost():
    from src.m5.grid import RoadGrid

    sim = world([(0, -73.999, 40.751), (1, -73.967, 40.762),
                 (2, -73.980, 40.751)], [(0, 1, 2)])
    grid = RoadGrid.build(sim.router)
    assert grid.bounds == (0, 0, 2, 1)
    assert grid.cell_count == 6
    assert grid.node_cells[2] == (1, 0)
    assert grid.entries.get((1, 0), ()) == ()
    assert grid.entries[(2, 1)] == (1,)
    assert grid.cost_m((0, 0), (0, 0)) == 0
    a, b = (radians(x) for x in grid.center((0, 0)))
    c, d = (radians(x) for x in grid.center((2, 1)))
    hav = sin((d-b)/2)**2 + cos(b)*cos(d)*sin((c-a)/2)**2
    assert grid.cost_m((0, 0), (2, 1)) == pytest.approx(
        2 * 6371009.0 * atan2(sqrt(hav), sqrt(1-hav)), abs=1e-7
    )
    assert RoadGrid.build(sim.router, 500).node_cells[1] == (5, 2)


def test_p5_config_reads_base_noR_and_independent_gurobi_options():
    from src.common.config import load_config
    from src.m5 import GurobiConfig, M5Config

    base = M5Config.from_config(load_config("configs/base.yaml"))
    assert base == M5Config()
    no_r = M5Config.from_config(load_config("configs/base.yaml", overrides={
        "m5": {"enabled": False, "grid_size_m": 500, "gurobi": {"seed": 23, "time_limit_s": 2}},
    }))
    assert no_r == M5Config(False, 500, GurobiConfig(23, 2))
    with pytest.raises(ValueError, match="solver"):
        M5Config.from_config({"m5": {"solver": "other"}})
