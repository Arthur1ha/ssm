"""Reachability diagnostics share the full-map observation selector."""
import math
import numpy as np
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE, OCCUPIED
from cloud.go2.navigation.frontier import raycast_directions, reachable_summary, select_observation_target
from cloud.go2.navigation.observation_coverage import ObservationCoverage


def room():
    grid = OccupancyGrid({'data': {'frame_id': 'odom', 'resolution': .1,
        'origin': [-4., -3., 0.], 'width': [80, 60, 1], 'data': {'points': []}}})
    grid.cells[:] = FREE
    grid.known_floor[:] = True
    grid.grid[:] = False
    grid.map_session = 's'
    return grid


POSE = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
BOUNDS = (-4., -3., 4., 3.)


def test_diagnostics_cover_whole_component_beyond_old_local_limit():
    grid = room()
    summary = reachable_summary(grid, POSE)
    assert summary['connected_cells'] == 4800
    assert summary['max_displacement_m'] > 4
    directions = raycast_directions(grid, POSE)
    assert directions['forward'] > 3
    assert directions['backward'] > 3
    assert max(directions.values()) < 5


def test_unknown_and_obstacles_disconnect_cells_without_corner_cutting():
    grid = room()
    grid.cells[:, 45] = OCCUPIED
    grid.grid[:, 45] = True
    assert reachable_summary(grid, POSE)['connected_cells'] == 45 * 60
    assert raycast_directions(grid, POSE)['forward'] < 1
    grid.cells[:] = 0
    grid.cells[30, 40] = grid.cells[31, 41] = FREE
    assert reachable_summary(grid, POSE)['connected_cells'] == 1


def test_invalid_or_unknown_start_has_no_reachable_cells():
    grid = room()
    assert reachable_summary(grid, {'x': 99., 'y': 0})['connected_cells'] == 0
    grid.cells[:] = 0
    assert reachable_summary(grid, POSE)['connected_cells'] == 0
    assert all(value == 0 for value in raycast_directions(grid, POSE).values())


def test_selected_target_stays_inside_explicit_scope_and_on_free_ground():
    grid = room()
    selection = select_observation_target(grid, POSE, ObservationCoverage('s'), bounds=(-1,-1,1,1))
    target = selection.target
    assert target is not None
    assert -1 <= target.x <= 1 and -1 <= target.y <= 1
    assert grid.is_free(*grid.odom_to_grid(target.x, target.y))


def test_conflicting_obstacle_requests_recheck_without_clearing_obstacle():
    grid = room()
    ix, iy = grid.odom_to_grid(1.2, .2)
    grid.obstacles[iy, ix] = True
    grid.cells[iy, ix] = OCCUPIED
    grid.grid[iy, ix] = True
    grid.pending_recheck = np.zeros_like(grid.obstacles)
    grid.pending_recheck[iy, ix] = True
    coverage = ObservationCoverage('s', bounds=BOUNDS)
    for gy, gx in zip(*np.nonzero(grid.cells == FREE)):
        x,y = grid.grid_to_odom(int(gx),int(gy))
        coverage._seen[(math.floor(x/grid.resolution + 1e-6),math.floor(y/grid.resolution + 1e-6))]=1
    selection = select_observation_target(grid, POSE, coverage, bounds=BOUNDS)
    assert selection.target is not None
    assert selection.target.source == 'pending_recheck'
    assert selection.reason == 'recheck_selected'
    assert grid.cells[iy, ix] == OCCUPIED and grid.obstacles[iy, ix]
