"""Full-room coverage, explicit scope, sweep geometry and stable decisions."""
import math
import numpy as np

from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE, UNKNOWN
from cloud.go2.navigation.frontier import select_observation_target
from cloud.go2.navigation.observation_coverage import ObservationCoverage, visible_cells


def room():
    grid = OccupancyGrid({'data': {'frame_id': 'odom', 'resolution': .1,
        'origin': [-4., -3., 0.], 'width': [80, 60, 1], 'data': {'points': []}}})
    grid.cells[2:-2, 2:-2] = FREE
    grid.known_floor = grid.cells == FREE
    grid.grid = grid.cells != FREE
    grid.map_session = 'a'
    return grid


BOUNDS = (-3.9, -2.9, 3.9, 2.9)
POSE = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'a'}


def test_unseen_view_beyond_old_distance_limit_can_be_selected():
    grid = room()
    coverage = ObservationCoverage('a', bounds=BOUNDS)
    # Unobserved useful views are on the far side of this traversable room.
    coverage.gain = lambda _grid, pose: 100 if pose['x'] > 1.6 else 0
    selection = select_observation_target(grid, POSE, coverage, bounds=BOUNDS)
    assert selection.target is not None
    assert math.hypot(selection.target.x, selection.target.y) > 1.5
    assert selection.target.source == 'visual_coverage'


def test_known_room_still_selects_unseen_camera_view_and_keeps_target_stable():
    grid = room()
    grid.cells[:] = FREE
    grid.known_floor[:] = True
    grid.grid[:] = False
    coverage = ObservationCoverage('a', bounds=BOUNDS)
    assert coverage.record(grid, POSE, 1., .9)
    first = select_observation_target(grid, POSE, coverage, bounds=BOUNDS)
    assert first.target is not None
    second = select_observation_target(grid, {**POSE, 'heading': .01}, coverage,
                                       bounds=BOUNDS, current=first.target)
    assert second.reason == 'target_retained'
    assert (second.target.x, second.target.y, second.target.heading) == (first.target.x, first.target.y, first.target.heading)


def test_same_frame_or_wrong_session_never_adds_coverage_and_walls_occlude():
    grid = room()
    grid.cells[:, 45] = 2
    coverage = ObservationCoverage('a', bounds=BOUNDS)
    assert coverage.record(grid, POSE, 1., .9)
    assert not coverage.record(grid, POSE, 1., .9)
    assert not coverage.record(grid, POSE, 2., .1)
    assert not coverage.record(grid, {**POSE, 'session_id': 'old'}, 2., .9)
    visible = visible_cells(grid, POSE, bounds=BOUNDS)
    assert all(x * grid.resolution < .6 for x, _ in visible)


def test_scope_is_required_and_turn_sweep_still_rejects_real_obstacles():
    grid = room()
    coverage = ObservationCoverage('a')
    assert select_observation_target(grid, POSE, coverage, bounds=None).reason == 'search_scope_required'
    ix, iy = grid.odom_to_grid(.3, .2)
    grid.obstacles[iy, ix] = True
    result = grid.turn_sweep_details(0., 0., 0., math.pi/2)
    assert not result['clear'] and 'measured_obstacle' in result['blockers']


def test_missing_floor_does_not_block_either_small_or_full_turn():
    grid = room()
    # The cell centered at y=.35 is inside the full turn disk but outside the
    # conservatively padded body rectangle for this three-hundredths turn.
    ix, iy = grid.odom_to_grid(0., .36)
    grid.known_floor[iy, ix] = False
    assert grid.is_turn_clear(0., 0.)
    assert grid.turn_sweep_details(0., 0., 0., .03)['clear']


def test_site_scope_selection_uses_same_body_margin_as_navigator():
    from cloud.go2.navigation.navigator import Navigator
    from cloud.go2.navigation.occupancy_grid import TURN_CLEARANCE_M

    grid = room()
    grid.cells[:] = FREE
    grid.known_floor[:] = True
    grid.grid[:] = False
    bounds = (-.490957, -.681815, 1.509043, .718185)
    pose = {**POSE, 'x': .009, 'y': .018, 'heading': .31}
    coverage = ObservationCoverage('a', bounds=bounds)
    assert coverage.record(grid, pose, 1., .9)
    navigator = Navigator()
    # This is the real rejected first candidate: its centre is in scope, but
    # its body would cross the permitted boundary.
    assert navigator._plan_path({'x': 1.3, 'y': -.15, 'bounds': bounds}, grid, pose) is None
    selected = select_observation_target(grid, pose, coverage, bounds=bounds)
    assert selected.target is not None
    target = selected.target
    plan = navigator._plan_path({'x': target.x, 'y': target.y, 'bounds': bounds}, grid, pose)
    assert plan is not None
    path = plan[0]
    for cell in path:
        x, y = grid.grid_to_odom(*cell)
        assert bounds[0] + TURN_CLEARANCE_M <= x <= bounds[2] - TURN_CLEARANCE_M
        assert bounds[1] + TURN_CLEARANCE_M <= y <= bounds[3] - TURN_CLEARANCE_M


def test_scope_too_narrow_for_body_has_no_observation_destination():
    grid = room()
    bounds = (-.3, -1., .3, 1.)
    result = select_observation_target(grid, POSE, ObservationCoverage('a', bounds=bounds),
                                       bounds=bounds)
    assert result.target is None
    assert result.reason == 'no_reachable_observation_point'


def test_start_filter_uses_planned_lookahead_not_direct_goal_bearing(monkeypatch):
    from types import SimpleNamespace
    from cloud.go2.navigation.frontier import ObservationTarget, _can_start_observation
    from cloud.go2.navigation.navigator import Navigator
    grid = room()
    # The target is straight ahead, but its route initially goes left.
    route = SimpleNamespace(grid_to_odom=lambda x, y: (y*.1, y*.7))
    monkeypatch.setattr(Navigator, '_plan_path', lambda *args:
                        ([(0, 0), (0, 1), (1, 1)], route, (1., 0., False)))
    monkeypatch.setattr(grid, 'turn_sweep_details', lambda *args: {'clear': False})
    target = ObservationTarget(1., 0., 0., 'visual_coverage', 1., 1., 'a')
    assert not _can_start_observation(grid, POSE, target, BOUNDS, {})


def test_unexecutable_views_get_measured_forward_reposition(monkeypatch):
    from types import SimpleNamespace
    grid = room()
    grid.cells[:] = FREE
    grid.known_floor[:] = True
    grid.grid[:] = False
    original = grid.turn_sweep_details
    def sweep(x, y, first, last):
        if math.hypot(x, y) < .01 and abs(last-first) > .02:
            return {'clear': False, 'blockers': ['unmeasured_floor']}
        return original(x, y, first, last)
    monkeypatch.setattr(grid, 'turn_sweep_details', sweep)
    # Only views to the left have unobserved content; every route starts turning.
    coverage = SimpleNamespace(map_session='a', records=[{}],
        gain=lambda grid, pose: 100 if pose['y'] > 1 and abs(pose['x']) < .5 else 0,
        visits=lambda pose: 0, failures=lambda target: 0)
    selected = select_observation_target(grid, POSE, coverage, bounds=BOUNDS)
    assert selected.reason == 'observation_reposition_selected'
    assert selected.target.source == 'observation_reposition'
    assert (selected.target.x, selected.target.y, selected.target.heading) == (.30, 0., 0.)
    # Missing floor does not block repositioning; a real obstacle still does.
    ix, iy = grid.odom_to_grid(.6, 0.)
    grid.known_floor[iy, ix] = False
    assert select_observation_target(grid, POSE, coverage, bounds=BOUNDS).target is not None
    grid.obstacles[iy, ix] = True
    blocked = select_observation_target(grid, POSE, coverage, bounds=BOUNDS)
    assert blocked.target is None
    assert blocked.reason == 'observation_start_unconfirmed'
