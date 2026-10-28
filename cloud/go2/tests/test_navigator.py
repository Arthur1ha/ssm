import asyncio
import math
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import pytest


@pytest.fixture
def mock_go2():
    m = MagicMock()
    m.is_connected    = True
    m.odom            = {"x": 0.0, "y": 0.0, "heading": 0.0}
    m.move_velocity   = MagicMock()
    m.occupancy_grid  = None
    m.global_path     = []
    return m


@pytest.fixture
def mock_memory():
    m = MagicMock()
    m.record_trajectory_tick = MagicMock()
    return m


def test_go_to_unknown_location(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    mock_memory.find_location = AsyncMock(return_value=None)
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        result = asyncio.run(nav.go_to("不存在"))
    assert result["status"] == "blocked"
    assert result["reason"] == "location_not_found"


def test_stop_cancels_patrol_task(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        nav._task = mock_task
        nav.stop()
    mock_task.cancel.assert_called_once()
    assert nav.mode.value == "idle"


def test_normalize_angle():
    from cloud.go2.navigation.navigator import _normalize_angle
    assert _normalize_angle(0.0) == pytest.approx(0.0)
    assert _normalize_angle(math.pi + 0.1) == pytest.approx(-math.pi + 0.1, abs=0.01)
    assert _normalize_angle(-math.pi - 0.1) == pytest.approx(math.pi - 0.1, abs=0.01)


def test_state_reflects_mode_and_target(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        nav.target = "窗边"
        state = nav.state
    assert state["target"] == "窗边"
    assert "mode" in state


def test_find_lookahead_on_path_selects_distant_point():
    from cloud.go2.navigation.navigator import _find_lookahead_on_path, LOOKAHEAD_DIST
    grid_obj = MagicMock()
    grid_obj.grid_to_odom = lambda ix, iy: (ix * 0.05, iy * 0.05)
    path = [(0, 0), (6, 0), (12, 0), (18, 0)]
    goal_loc = {"x": 0.9, "y": 0.0}
    # 朝向 0（正 X 方向），路径全在前方
    wx, wy = _find_lookahead_on_path(path, grid_obj, 0.0, 0.0, 0.0, goal_loc)
    assert wx == pytest.approx(0.6), f"期望 wx=0.6，实际 {wx}"
    assert wy == pytest.approx(0.0)


def test_find_lookahead_on_path_falls_back_to_goal():
    from cloud.go2.navigation.navigator import _find_lookahead_on_path
    grid_obj = MagicMock()
    grid_obj.grid_to_odom = lambda ix, iy: (ix * 0.05, iy * 0.05)
    path = [(0, 0), (3, 0), (6, 0)]
    goal_loc = {"x": 0.4, "y": 0.1}
    wx, wy = _find_lookahead_on_path(path, grid_obj, 0.0, 0.0, 0.0, goal_loc)
    assert wx == pytest.approx(0.4)
    assert wy == pytest.approx(0.1)


def test_find_lookahead_on_path_ignores_behind_points():
    from cloud.go2.navigation.navigator import _find_lookahead_on_path
    grid_obj = MagicMock()
    grid_obj.grid_to_odom = lambda ix, iy: (ix * 0.05, iy * 0.05)
    # 路径全在机器人身后（负 X 方向），朝向 0 = 正 X
    path = [(-18, 0), (-12, 0), (-6, 0)]
    goal_loc = {"x": 0.5, "y": 0.0}
    # 身后点都应被过滤，最终回退到 goal_loc
    wx, wy = _find_lookahead_on_path(path, grid_obj, 0.0, 0.0, 0.0, goal_loc)
    assert wx == pytest.approx(0.5)
    assert wy == pytest.approx(0.0)
