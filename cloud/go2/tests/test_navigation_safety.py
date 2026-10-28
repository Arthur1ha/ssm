"""Offline navigation safety boundaries; no robot connection is used."""
import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from cloud.go2.navigation.astar import astar
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE, UNKNOWN
from cloud.go2.navigation import navigator as nav_module


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
