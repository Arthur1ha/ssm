"""Evidence fusion and bounded receiver work, without a robot connection."""
import threading
import numpy as np
import pytest

from cloud.go2.navigation.session_map import SessionMap
from cloud.go2.navigation.occupancy_grid import FREE, OCCUPIED


def message(*, origin=(-2., -2., 0.), obstacle=False):
    points = [[x * .1, y * .1, -.3] for x in range(-15, 16) for y in range(-15, 16)]
    if obstacle:
        points.append([.8, 0., 0.])
    return {'data': {'frame_id': 'odom', 'resolution': .1, 'origin': origin,
                     'width': [40, 40, 10], 'data': {'points': np.array(points)}}}


def test_accumulates_moving_window_and_keeps_atomic_readonly_snapshots():
    maps = SessionMap()
    first = maps.update(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    second = maps.update(message(origin=(-1., -2., 0.)), {'x': 0., 'y': 0.}, session_id='a', observed_at=2.)
    assert second.version == first.version + 1
    assert second.width[0] == 50
    assert first.width[0] == 40
    assert second.is_free(*second.odom_to_grid(-1.4, 0))
    with pytest.raises(ValueError):
        first.cells[0, 0] = FREE


def test_current_window_clears_departed_obstacle_and_its_inflation():
    maps = SessionMap()
    maps.update(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    occupied = maps.update(message(obstacle=True), {'x': 0., 'y': 0.}, session_id='a', observed_at=2.)
    ix, iy = occupied.odom_to_grid(.85, .05)
    assert occupied.cells[iy, ix] == OCCUPIED
    recheck = maps.update(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=3.)
    assert recheck.cells[iy, ix] == FREE
    assert not recheck.obstacles[iy, ix]
    assert not recheck.pending_recheck[iy, ix]
    assert not np.any(recheck.obstacles)
    assert not np.any(recheck.cells == OCCUPIED)


def test_obstacles_outside_new_window_remain_until_window_covers_them():
    maps = SessionMap()
    first = maps.update(message(obstacle=True), {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    new = message(origin=(2., -2., 0.))
    new['data']['data']['points'][:, 0] += 4.
    second = maps.update(new, {'x': 4., 'y': 0.}, session_id='a', observed_at=2.)
    assert second.is_free(*second.odom_to_grid(4., 0.))
    ix, iy = second.odom_to_grid(.85, .05)
    assert second.cells[iy, ix] == OCCUPIED
    assert first.cells[first.odom_to_grid(.85,.05)[1], first.odom_to_grid(.85,.05)[0]] == OCCUPIED


def test_empty_or_invalid_cloud_does_not_erase_map_or_refresh_its_age():
    maps = SessionMap()
    first = maps.update(message(obstacle=True), {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    for points in (np.empty((0, 3)), np.array([[np.nan, np.nan, np.nan]]),
                   np.array([[1000., 1000., 0.]])):
        missing = message()
        missing['data']['data']['points'] = points
        assert maps.update(missing, {'x': 0., 'y': 0.}, session_id='a', observed_at=2.) is first
        assert maps.last_error == 'empty_voxel_observation'
        assert first.observed_at == 1.


def test_session_reset_resolution_rejection_and_memory_limit_do_not_reuse_coordinates():
    maps = SessionMap(max_cells=1900)
    first = maps.update(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    assert maps.update(message(origin=(-1., -2., 0.)), {'x': 0., 'y': 0.}, session_id='a', observed_at=2.) is None
    assert maps.last_error == 'map_memory_limit'
    assert maps.snapshot() is first
    maps.reset('b')
    assert maps.snapshot() is None
    second = maps.update(message(), {'x': 0., 'y': 0.}, session_id='b', observed_at=3.)
    assert second.version == 1 and second.session_id == 'b'


def test_missing_cell_is_immediately_free_without_fabricating_observation():
    msg = message()
    points = msg['data']['data']['points']
    msg['data']['data']['points'] = points[~((abs(points[:, 0]-.8)<.001) & (abs(points[:, 1])<.001))]
    maps = SessionMap()
    first = maps.update(msg, {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    cell = first.odom_to_grid(.85, .05)
    assert first.is_free(*cell)
    assert first.floor_observations[cell[1], cell[0]] == 0
    second = maps.update(msg, {'x': 0., 'y': 0.}, session_id='a', observed_at=2.)
    assert second.is_free(*cell)
    assert second.floor_observations[cell[1], cell[0]] == 0


def test_receiver_replaces_pending_frame_and_old_generation_cannot_publish(monkeypatch):
    import cloud.go2.navigation.session_map as module
    entered, release = threading.Event(), threading.Event()
    original = module.OccupancyGrid
    def delayed(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'OccupancyGrid', delayed)
    maps = SessionMap()
    maps.submit(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=1.)
    assert entered.wait(1)
    maps.submit(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=2.)
    maps.submit(message(), {'x': 0., 'y': 0.}, session_id='a', observed_at=3.)
    assert maps.dropped_frames == 1
    maps.reset('b')
    release.set()
    maps._executor.shutdown(wait=True)
    assert maps.snapshot() is None
