import numpy as np
import pytest
from cloud.go2.navigation.occupancy_grid import OccupancyGrid

def _make_msg(positions: list[list[int]]) -> dict:
    return {
        "data": {
            "resolution": 0.05,
            "origin": [-3.225, -3.175, -0.575],
            "width": [128, 128, 38],
            "data": {"positions": np.array(positions, dtype=np.uint8)},
        }
    }

def test_obstacle_appears_in_grid():
    msg = _make_msg([[70, 60, 15]])
    grid = OccupancyGrid(msg)
    assert grid.grid[60, 70] == True

def test_floor_voxel_ignored():
    msg = _make_msg([[70, 60, 2]])
    grid = OccupancyGrid(msg)
    assert grid.grid[60, 70] == False

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

def test_obstacle_inflation():
    msg = _make_msg([[64, 64, 15]])
    grid = OccupancyGrid(msg)
    assert grid.grid[64, 65] == True
