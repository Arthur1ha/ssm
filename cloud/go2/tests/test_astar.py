import numpy as np
import pytest
from cloud.go2.astar import astar

def _free_grid(nx=20, ny=20) -> np.ndarray:
    return np.zeros((ny, nx), dtype=bool)

def test_straight_path():
    grid = _free_grid()
    path = astar(grid, (0, 0), (5, 0))
    assert path is not None
    assert path[0] == (0, 0)
    assert path[-1] == (5, 0)

def test_no_path_when_blocked():
    grid = _free_grid()
    grid[:, 5] = True  # vertical wall
    path = astar(grid, (0, 0), (10, 0))
    assert path is None

def test_path_goes_around_obstacle():
    grid = _free_grid(20, 20)
    grid[5:15, 8] = True  # middle vertical wall with gaps at top and bottom
    path = astar(grid, (5, 0), (5, 19))
    assert path is not None
    blocked_cells = [(8, iy) for iy in range(5, 15)]
    for cell in path:
        assert cell not in blocked_cells

def test_start_equals_goal():
    grid = _free_grid()
    path = astar(grid, (3, 3), (3, 3))
    assert path == [(3, 3)]
