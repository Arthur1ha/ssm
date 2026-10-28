"""Offline navigation safety boundaries; no robot connection is used."""
import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.navigation.astar import astar
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE, UNKNOWN
from cloud.go2.navigation import navigator as nav_module
from cloud.go2.navigation import frontier as frontier_module
from cloud.go2.navigation.frontier import (
    _raycast, find_exploration_target, find_frontier_target, raycast_directions,
)


def _voxel(points=None):
    return {"data": {"resolution": 0.05, "origin": [-1.0, -1.0, 0.0],
                     "width": [40, 40, 38], "frame_id": "odom",
                     "data": {"points": np.asarray(points if points is not None else [], dtype=float)}}}


def test_voxel_without_free_evidence_remains_unknown():
    grid = OccupancyGrid(_voxel([[0.8, 0.8, 0.5]]), robot_pose={"x": 0, "y": 0})
    assert grid.cells[4, 4] == UNKNOWN
    assert not grid.is_free(4, 4)
    assert grid.cells[20, 20] == FREE
    assert grid.grid[4, 4]
    assert grid.has_free_space_evidence is False
    with pytest.raises(ValueError):
        grid.odom_to_grid(1.0, 0.0)


def test_measured_floor_opens_short_exploration_path_but_unknown_stays_blocked():
    floor = [[x / 20, y / 20, -0.3]
             for x in range(-6, 21) for y in range(-8, 9)]
    grid = OccupancyGrid(_voxel(floor), robot_pose={"x": 0, "y": 0})
    assert grid.has_free_space_evidence
    target = find_exploration_target(grid, {"x": 0, "y": 0, "heading": 0}, "forward")
    assert target
    assert astar(grid.grid, grid.odom_to_grid(0, 0), grid.odom_to_grid(*target))
    assert not grid.is_free(*grid.odom_to_grid(-0.85, 0))


def test_nearby_measured_floor_bridges_only_local_sensor_blind_gap():
    floor = [[x * 0.05, y * 0.05, -0.3]
             for x in range(-16, 21) for y in range(-12, 13)
             if (x * 0.05) ** 2 + (y * 0.05) ** 2 >= 0.38 ** 2]
    grid = OccupancyGrid(_voxel(floor), robot_pose={"x": 0, "y": 0})
    summary = raycast_directions(grid, {"x": 0, "y": 0, "heading": 0})
    assert summary["forward"] >= 0.6
    assert find_frontier_target(grid, {"x": 0, "y": 0, "heading": 0})
    assert grid.cells[20, 20] == FREE
    assert grid.cells[20, 36] == FREE  # measured floor beyond the blind gap


def test_frontier_prefers_reachable_forward_target_before_rear_turn(monkeypatch):
    cells = np.full((40, 40), FREE, dtype=np.uint8)
    cells[18:23, 5:8] = UNKNOWN    # larger unknown patch behind the robot
    cells[20, 32] = UNKNOWN        # smaller frontier ahead
    grid = SimpleNamespace(
        cells=cells, width=(40, 40),
        odom_to_grid=lambda x, y: (int((x + 1) / 0.05), int((y + 1) / 0.05)),
    )
    monkeypatch.setattr(frontier_module, "_reachable_candidates", lambda _grid, _odom: {
        "forward": [(0.55, 0.55, 0.0)],
        "backward": [(0.55, -0.55, 0.0)],
    })
    assert find_frontier_target(grid, {"x": 0.0, "y": 0.0, "heading": 0.0}) == (0.55, 0.0)


def test_frontier_does_not_request_rear_turn_when_body_cannot_spin(monkeypatch):
    cells = np.full((40, 40), FREE, dtype=np.uint8)
    cells[18:23, 5:8] = UNKNOWN
    grid = SimpleNamespace(
        cells=cells, width=(40, 40), is_turn_clear=lambda *_: False,
        odom_to_grid=lambda x, y: (int((x + 1) / 0.05), int((y + 1) / 0.05)),
    )
    monkeypatch.setattr(frontier_module, "_reachable_candidates", lambda _grid, _odom: {
        "backward": [(0.55, -0.55, 0.0)],
    })
    assert find_frontier_target(grid, {"x": 0.0, "y": 0.0, "heading": 0.0}) is None


def test_local_floor_bridge_does_not_clear_obstacle():
    floor = [[x * 0.05, y * 0.05, -0.3]
             for x in range(-16, 21) for y in range(-12, 13)
             if (x * 0.05) ** 2 + (y * 0.05) ** 2 >= 0.38 ** 2]
    grid = OccupancyGrid(_voxel(floor + [[0.36, 0, 0.1]]),
                         robot_pose={"x": 0, "y": 0})
    assert not grid.is_free(*grid.odom_to_grid(0.36, 0))


def test_measured_floor_does_not_clear_inflated_obstacle():
    floor = [[x / 20, y / 20, -0.3]
             for x in range(-6, 21) for y in range(-8, 9)]
    grid = OccupancyGrid(_voxel(floor + [[0.55, 0, 0.3]]), robot_pose={"x": 0, "y": 0})
    assert not grid.is_free(*grid.odom_to_grid(0.55, 0))
    target = find_exploration_target(grid, {"x": 0, "y": 0, "heading": 0}, "forward")
    if target is not None:
        path = astar(grid.grid, grid.odom_to_grid(0, 0), grid.odom_to_grid(*target))
        assert path and all(grid.is_free(*cell) for cell in path)


def test_narrow_straight_corridor_uses_width_clearance_but_rejects_turn():
    floor = [[x * 0.05, y * 0.05, -0.3]
             for x in range(-6, 29) for y in range(-6, 7)]
    walls = [[x * 0.05, y * 0.3, 0.3]
             for x in range(-6, 29) for y in (-1, 1)]
    voxel = {"data": {"resolution": 0.05, "origin": [-1, -1, 0],
                      "width": [60, 60, 38], "frame_id": "odom",
                      "data": {"points": np.asarray(floor + walls)}}}
    grid = OccupancyGrid(voxel, robot_pose={"x": 0, "y": 0})
    odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    assert raycast_directions(grid, odom)["forward"] > 1.0
    assert find_frontier_target(grid, odom) is not None
    assert grid.is_body_clear(0, 0, 0)
    assert not grid.is_body_clear(0, 0, np.pi / 2)
    assert not grid.is_turn_clear(0, 0)


def test_exploration_uses_connected_route_around_bend():
    """A visible known-free route can turn around a wall before heading forward."""
    resolution = 0.05
    origin = (-1.5, -1.5)
    blocked = np.ones((60, 60), dtype=bool)
    for iy in range(60):
        y = origin[1] + (iy + 0.5) * resolution
        for ix in range(60):
            x = origin[0] + (ix + 0.5) * resolution
            if -0.15 <= x <= 1.0 and -0.15 <= y <= 0.55:
                blocked[iy, ix] = False
            if 0.25 <= x <= 0.35 and -0.15 <= y <= 0.3:
                blocked[iy, ix] = True
    def odom_to_grid(x, y):
        return int((x - origin[0]) // resolution), int((y - origin[1]) // resolution)
    grid = SimpleNamespace(
        resolution=resolution, origin=origin, width=(60, 60), grid=blocked,
        odom_to_grid=odom_to_grid,
        grid_to_odom=lambda ix, iy: (origin[0] + (ix + 0.5) * resolution,
                                    origin[1] + (iy + 0.5) * resolution),
        is_free=lambda ix, iy: 0 <= ix < 60 and 0 <= iy < 60 and not blocked[iy, ix],
    )
    odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    assert _raycast(grid, 0.0, 0.0, 0.0) < 0.5
    distances = raycast_directions(grid, odom)
    assert distances["forward"] >= 0.625
    target = find_exploration_target(grid, odom, "forward")
    assert target is not None and target[0] > 0.5
    assert astar(grid.grid, grid.odom_to_grid(0, 0), grid.odom_to_grid(*target))
    # A detached island of free cells is not offered as a destination.
    blocked[:, 38:] = True
    assert find_exploration_target(grid, odom, "forward") is None


def test_astar_rejects_blocked_endpoints_and_corner_cutting():
    blocked = np.array([[False, True], [True, False]])
    assert astar(blocked, (0, 0), (1, 1)) is None
    assert astar(blocked, (1, 0), (1, 0)) is None


@pytest.mark.parametrize("failure", ["no_map", "stale_map", "stale_pose", "wrong_frame",
                                      "unknown_path", "blocked_goal", "outside_goal"])
def test_navigation_failure_never_sends_forward(monkeypatch, failure):
    now = time.monotonic()
    commands = []
    points = [[0.8, 0.0, 0.5]] if failure == "blocked_goal" else None
    grid = (OccupancyGrid(_voxel(points), robot_pose={"x": 0, "y": 0})
            if failure == "unknown_path" else _free_grid())
    if failure == "blocked_goal":
        grid.grid[:, 27] = True  # barrier separates robot from goal's free region
        grid.grid[20, 36] = True
    robot = SimpleNamespace(
        is_connected=True, odom={"x": 0.0, "y": 0.0, "heading": 0.0},
        odom_received_at=now, voxel_received_at=now,
        odom_frame_id="odom", voxel_frame_id="odom",
        _odom_session_id="session", occupancy_grid=grid,
        move_velocity=lambda x, y, yaw, **kwargs: commands.append((x, y, yaw)),
    )
    if failure == "no_map":
        robot.occupancy_grid = None
    elif failure == "stale_map":
        robot.voxel_received_at = now - 10
    elif failure == "stale_pose":
        robot.odom_received_at = now - 10
    elif failure == "wrong_frame":
        robot.voxel_frame_id = "map"
    monkeypatch.setattr(nav_module, "go2", robot)

    async def location(_name):
        return {"name": "door", "x": 2.0 if failure == "outside_goal" else 0.8,
                "y": 0.0, "heading": None,
                "session_id": "session", "frame_id": "odom"}

    monkeypatch.setattr(nav_module.spatial_memory, "find_location", location)
    nav = nav_module.Navigator()
    result = asyncio.run(nav.go_to("door"))
    assert result["status"] in {"blocked", "failed"}
    expected = {
        "no_map": "map_unavailable", "stale_map": "map_unavailable",
        "stale_pose": "localization_unavailable", "wrong_frame": "frame_mismatch",
        "unknown_path": "map_unavailable", "blocked_goal": "no_known_free_path",
        "outside_goal": "no_known_free_path",
    }
    assert result["reason"] == expected[failure]
    assert nav.task_result(result["task_id"])["status"] == result["status"]
    assert commands
    assert all(x == 0 for x, _, _ in commands)


def test_old_location_cannot_be_used_after_reconnect(monkeypatch):
    commands = []
    monkeypatch.setattr(nav_module, "go2", SimpleNamespace(
        _odom_session_id="new", move_velocity=lambda *args, **kwargs: commands.append(args)))

    async def location(_name):
        return {"name": "home", "x": 0, "y": 0, "heading": 0,
                "session_id": "old", "frame_id": "odom"}

    monkeypatch.setattr(nav_module.spatial_memory, "find_location", location)
    result = asyncio.run(nav_module.Navigator().go_to("home"))
    assert result["status"] == "blocked"
    assert result["reason"] == "map_mismatch"
    assert all(x == 0 for x, _, _ in commands)


def _free_grid():
    """Synthetic validated free map, used only to exercise task control paths."""
    cells = np.zeros((40, 40), dtype=bool)
    def odom_to_grid(x, y):
        ix, iy = int((x + 1.0) / 0.05), int((y + 1.0) / 0.05)
        if not (0 <= ix < 40 and 0 <= iy < 40):
            raise ValueError("outside map")
        return ix, iy
    return SimpleNamespace(
        frame_id="odom", has_free_space_evidence=True, resolution=0.05, grid=cells,
        odom_to_grid=odom_to_grid,
        grid_to_odom=lambda x, y: (-1.0 + (x + 0.5) * 0.05, -1.0 + (y + 0.5) * 0.05),
        is_free=lambda x, y: 0 <= x < 40 and 0 <= y < 40 and not cells[y, x],
    )


def _synthetic_robot(commands):
    now = time.monotonic()
    return SimpleNamespace(
        is_connected=True, odom={"x": 0.0, "y": 0.0, "heading": 0.0},
        odom_received_at=now, voxel_received_at=now,
        odom_frame_id="odom", voxel_frame_id="odom",
        _odom_session_id="session", occupancy_grid=_free_grid(),
        move_velocity=lambda x, y, yaw, **kwargs: commands.append((x, y, yaw, kwargs)),
    )


def test_navigation_velocity_has_deadman_and_timeout_stops(monkeypatch):
    commands = []
    robot = _synthetic_robot(commands)
    monkeypatch.setattr(nav_module, "go2", robot)
    monkeypatch.setattr(nav_module, "NAV_TIMEOUT", 0.25)
    monkeypatch.setattr(nav_module, "POSE_STALE_S", 10.0)
    monkeypatch.setattr(nav_module, "MAP_STALE_S", 10.0)
    result = asyncio.run(nav_module.Navigator().go_to_coordinate(0.8, 0.0))
    assert result["reason"] == "navigation_timeout"
    assert any(x > 0 and kw.get("deadman_s") == 0.3 for x, _, _, kw in commands)
    assert commands[-1][:3] == (0, 0, 0)


def test_navigation_rejects_turn_without_body_clearance(monkeypatch):
    commands = []
    robot = _synthetic_robot(commands)
    robot.odom["heading"] = np.pi / 2
    robot.occupancy_grid.is_body_clear = lambda *_: True
    robot.occupancy_grid.is_turn_clear = lambda *_: False
    monkeypatch.setattr(nav_module, "go2", robot)
    result = asyncio.run(nav_module.Navigator().go_to_coordinate(0.8, 0.0))
    assert result["status"] == "blocked"
    assert result["reason"] == "turn_clearance_unavailable"
    assert commands and all(x == 0 for x, _, _, _ in commands)


def test_stop_cancels_navigation_and_sends_zero(monkeypatch):
    commands = []
    robot = _synthetic_robot(commands)
    monkeypatch.setattr(nav_module, "go2", robot)
    monkeypatch.setattr(nav_module, "POSE_STALE_S", 10.0)
    monkeypatch.setattr(nav_module, "MAP_STALE_S", 10.0)

    async def scenario():
        nav = nav_module.Navigator()
        task_id = nav.start_go_to("door", owner_session_id="owner")
        await asyncio.sleep(0.15)
        assert nav.stop(owner_session_id="owner")
        result = await nav._task
        assert result["task_id"] == task_id
        assert result["status"] == "cancelled"
        assert nav.state["status"] == "cancelled"

    async def location(_name):
        return {"name": "door", "x": 0.8, "y": 0.0, "heading": None,
                "session_id": "session", "frame_id": "odom"}

    monkeypatch.setattr(nav_module.spatial_memory, "find_location", location)
    asyncio.run(scenario())
    assert commands[-1][:3] == (0, 0, 0)


def test_single_pose_frame_cannot_confirm_arrival(monkeypatch):
    commands = []
    robot = _synthetic_robot(commands)
    monkeypatch.setattr(nav_module, "go2", robot)
    monkeypatch.setattr(nav_module, "POSE_STALE_S", 10.0)
    monkeypatch.setattr(nav_module, "MAP_STALE_S", 10.0)
    result = asyncio.run(nav_module.Navigator().go_to_coordinate(0.0, 0.0))
    assert result["status"] == "failed"
    assert result["reason"] == "arrival_unconfirmed"
    assert all(x == 0 for x, _, _, _ in commands)


def test_arrival_confirmation_waits_for_deceleration_then_fresh_stable_pose(monkeypatch):
    commands = []
    robot = _synthetic_robot(commands)
    monkeypatch.setattr(nav_module, "go2", robot)

    async def scenario():
        async def odom_updates():
            for x in (0.08, 0.16, 0.19, 0.19, 0.19, 0.19):
                await asyncio.sleep(0.12)
                robot.odom = {"x": x, "y": 0.0, "heading": 0.0}
                robot.odom_received_at = time.monotonic()

        nav = nav_module.Navigator()
        update = asyncio.create_task(odom_updates())
        confirmed = await nav._confirm_stopped((0.2, 0.0), None)
        await update
        return confirmed, nav.last_trusted_pose

    confirmed, pose = asyncio.run(scenario())
    assert confirmed
    assert pose["x"] == 0.19
    assert commands[0][:3] == (0, 0, 0)


def test_malformed_pose_is_not_substituted_with_zero_coordinates():
    from cloud.go2.connection.sensors import Go2Sensors
    sensors = Go2Sensors()
    sensors._on_odom({"data": {"header": {"frame_id": "odom"},
                                "pose": {"position": {"x": 0, "y": 0},
                                         "orientation": {"x": 0, "y": 0, "z": 0, "w": 1}}}})
    assert sensors.odom_received_at is not None
    sensors._on_odom({"data": {"header": {"frame_id": "odom"},
                                "pose": {"position": {"x": float("nan"), "y": 0}}}})
    assert sensors.odom == {}
    assert sensors.odom_received_at is None


def test_reconnect_clears_previous_path_overlay():
    from cloud.go2.connection.sensors import Go2Sensors
    sensors = Go2Sensors()
    sensors._global_path = [{"x": 1.0, "y": 0.0}]
    sensors.reset_sensor_epoch()
    assert sensors.global_path == []


def test_map_debug_serializes_numpy_sample(monkeypatch):
    from cloud.go2 import router as go2_router
    from cloud.go2.execution import ExecutionContext
    monkeypatch.setattr(go2_router, "_require_admin_persist", lambda _context: None)
    monkeypatch.setattr(go2_router, "go2", SimpleNamespace(
        voxel_raw={"data": {"points": np.array([[1.0, 2.0, 3.0]], dtype=np.float32),
                            "count": np.int64(1)}},
        sensor_diagnostics={"rate_hz": np.float64(2.0)},
    ))
    result = asyncio.run(go2_router.go2_map_debug(ExecutionContext("admin", "admin", True)))
    assert result["voxel"]["data"]["points"]["__shape__"] == [1, 3]
    assert result["diagnostics"]["rate_hz"] == 2.0
