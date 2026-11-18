import numpy as np
import pytest
from cloud.go2.navigation.occupancy_grid import OccupancyGrid

def _make_msg(points: list[list[float]]) -> dict:
    return {
        "data": {
            "resolution": 0.05,
            "origin": [-3.225, -3.175, -0.575],
            "width": [128, 128, 38],
            "data": {"points": np.array(points, dtype=float)},
        }
    }

def test_odom_to_grid():
    msg = _make_msg([])
    grid = OccupancyGrid(msg)
    ix, iy = grid.odom_to_grid(0.0, 0.0)
    assert ix == 64  # (0.0 - (-3.225)) / 0.05 ≈ 64.5 → 64
    assert iy == 63

def test_grid_to_odom():
    msg = _make_msg([])
    grid = OccupancyGrid(msg)
    x, y = grid.grid_to_odom(64, 63)
    assert abs(x - 0.0) < 0.06
    assert abs(y - 0.0) < 0.06
