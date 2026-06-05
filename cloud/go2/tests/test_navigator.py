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
    m.is_connected = True
    m.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}
    m.move_velocity = MagicMock()
    return m


@pytest.fixture
def mock_memory():
    m = MagicMock()
    return m


def test_go_to_unknown_location(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    mock_memory.find_location = AsyncMock(return_value=None)
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        result = asyncio.run(nav.go_to("不存在"))
    assert "找不到" in result


def test_go_to_already_at_destination(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    loc = {"name": "门口", "x": 0.1, "y": 0.0, "heading": 0.0}
    mock_memory.find_location = AsyncMock(return_value=loc)
    mock_memory.record_trajectory_tick = MagicMock()
    mock_go2.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}  # dist=0.1 < 0.3
    mock_go2.occupancy_grid = None
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        nav._align_heading = AsyncMock()
        result = asyncio.run(nav.go_to("门口"))
    assert "到达" in result
    mock_go2.move_velocity.assert_called_with(0, 0, 0)


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
    # 朝向已对齐，只需停止，不应发出转向指令
    for call in mock_go2.move_velocity.call_args_list:
        vx, vy, vyaw = call.args
        assert vyaw == 0.0, f"不应转向，但发出了 vyaw={vyaw}"


def test_align_heading_turns_to_correct_direction(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    # 初始朝向 0，目标朝向 +1.0（左转，正方向）
    odometry = [
        {"x": 0.0, "y": 0.0, "heading": 0.0},   # 第一次读取：需要转
        {"x": 0.0, "y": 0.0, "heading": 0.9},   # 第二次读取：接近目标
        {"x": 0.0, "y": 0.0, "heading": 1.0},   # 第三次读取：已到达
    ]
    odom_iter = iter(odometry)
    type(mock_go2).odom = property(lambda self: next(odom_iter, odometry[-1]))
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        asyncio.run(nav._align_heading(1.0))
    # 至少有一次正转向指令（vyaw > 0）
    turn_calls = [c for c in mock_go2.move_velocity.call_args_list if c.args[2] > 0]
    assert len(turn_calls) > 0, "应发出正方向（左转）转向指令"
    # 最后一次必须是停止
    last = mock_go2.move_velocity.call_args_list[-1]
    assert last.args == (0, 0, 0), "对齐完成后应停止"


def test_go_to_aligns_heading_on_arrival(mock_go2, mock_memory):
    from cloud.go2.navigation import navigator as nav_module
    loc = {"name": "充电桩", "x": 0.1, "y": 0.0, "heading": 1.57}
    mock_memory.find_location = AsyncMock(return_value=loc)
    mock_memory.record_trajectory_tick = MagicMock()
    mock_go2.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}  # dist=0.1 < 0.3
    mock_go2.occupancy_grid = None
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        # 将 _align_heading 替换为记录调用的 mock
        nav._align_heading = AsyncMock()
        result = asyncio.run(nav.go_to("充电桩"))
    assert "到达" in result
    nav._align_heading.assert_called_once_with(1.57)
