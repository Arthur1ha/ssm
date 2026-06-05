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
    assert "找不到" in result


def _fast_time():
    """返回递增时间：每次调用 +20s，让 go_to 里的 10s 等待立刻超时。"""
    t = [0.0]
    def _inner():
        t[0] += 20.0
        return t[0]
    return _inner


def test_go_to_already_at_destination(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    loc = {"name": "门口", "x": 0.1, "y": 0.0}
    mock_memory.find_location = AsyncMock(return_value=loc)
    mock_go2.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}  # dist=0.1 < 0.3

    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("cloud.go2.navigation.navigator.time") as mock_time:
        mock_time.monotonic = _fast_time()
        nav = nav_module.Navigator()
        nav._align_heading = AsyncMock()
        result = asyncio.run(nav.go_to("门口"))

    assert "到达" in result
    mock_go2.move_velocity.assert_called_with(0, 0, 0)


def test_go_to_aligns_heading_on_arrival(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    loc = {"name": "充电桩", "x": 0.1, "y": 0.0, "heading": 1.57}
    mock_memory.find_location = AsyncMock(return_value=loc)
    mock_go2.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}  # dist=0.1 < 0.3

    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory), \
         patch("asyncio.sleep", new=AsyncMock()), \
         patch("cloud.go2.navigation.navigator.time") as mock_time:
        mock_time.monotonic = _fast_time()
        nav = nav_module.Navigator()
        nav._align_heading = AsyncMock()
        result = asyncio.run(nav.go_to("充电桩"))

    assert "到达" in result
    nav._align_heading.assert_called_once_with(1.57)


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


def test_align_heading_no_turn_when_already_aligned(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    mock_go2.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        asyncio.run(nav._align_heading(0.0))
    for call in mock_go2.move_velocity.call_args_list:
        vx, vy, vyaw = call.args
        assert vyaw == 0.0, f"不应转向，但发出了 vyaw={vyaw}"


def test_align_heading_turns_to_correct_direction(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    odometry = [
        {"x": 0.0, "y": 0.0, "heading": 0.0},
        {"x": 0.0, "y": 0.0, "heading": 0.9},
        {"x": 0.0, "y": 0.0, "heading": 1.0},
    ]
    odom_iter = iter(odometry)
    type(mock_go2).odom = property(lambda self: next(odom_iter, odometry[-1]))
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        asyncio.run(nav._align_heading(1.0))
    turn_calls = [c for c in mock_go2.move_velocity.call_args_list if c.args[2] > 0]
    assert len(turn_calls) > 0, "应发出正方向（左转）转向指令"
    last = mock_go2.move_velocity.call_args_list[-1]
    assert last.args == (0, 0, 0), "对齐完成后应停止"


def test_navigate_to_does_not_forward_before_aligned(mock_go2, mock_memory):
    """initial_rotation 阶段只转向，对齐后才前进。"""
    from cloud.go2.navigation import navigator as nav_module

    odom_seq = [
        {"x": 0.0, "y": 0.0, "heading": 0.0},
        {"x": 0.0, "y": 0.0, "heading": 0.0},
        {"x": 0.0, "y": 0.0, "heading": 1.57},
        {"x": 0.0, "y": 1.8,  "heading": 1.57},
    ]
    odom_iter = iter(odom_seq)
    type(mock_go2).odom = property(lambda self: next(odom_iter, odom_seq[-1]))
    mock_memory.record_trajectory_tick = MagicMock()

    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory), \
         patch("asyncio.sleep", new=AsyncMock()):
        nav = nav_module.Navigator()
        nav.mode = nav_module.NavMode.GOING_TO
        nav._align_heading = AsyncMock()
        loc = {"name": "目标", "x": 0.0, "y": 2.0, "heading": 1.57}
        result = asyncio.run(nav._navigate_to(loc))

    assert "到达" in result
    calls = mock_go2.move_velocity.call_args_list
    # 第一次调用：转向（vx=0）
    assert calls[0].args[0] == 0.0, f"转向阶段不应前进，实际 vx={calls[0].args[0]}"


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
