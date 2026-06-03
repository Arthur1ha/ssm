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
    from cloud.go2 import navigator as nav_module
    mock_memory.find_location = AsyncMock(return_value=None)
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        result = asyncio.run(nav.go_to("不存在"))
    assert "找不到" in result


def test_go_to_already_at_destination(mock_go2, mock_memory):
    from cloud.go2 import navigator as nav_module
    loc = {"name": "门口", "x": 0.1, "y": 0.0, "heading": 0.0}
    mock_memory.find_location = AsyncMock(return_value=loc)
    mock_memory.record_trajectory_tick = MagicMock()
    mock_go2.odom = {"x": 0.0, "y": 0.0, "heading": 0.0}  # dist=0.1 < 0.3
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        result = asyncio.run(nav.go_to("门口"))
    assert "到达" in result
    mock_go2.move_velocity.assert_called_with(0, 0, 0)


def test_stop_cancels_patrol_task(mock_go2, mock_memory):
    from cloud.go2 import navigator as nav_module
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
    from cloud.go2.navigator import _normalize_angle
    assert _normalize_angle(0.0) == pytest.approx(0.0)
    assert _normalize_angle(math.pi + 0.1) == pytest.approx(-math.pi + 0.1, abs=0.01)
    assert _normalize_angle(-math.pi - 0.1) == pytest.approx(math.pi - 0.1, abs=0.01)


def test_state_reflects_mode_and_target(mock_go2, mock_memory):
    from cloud.go2 import navigator as nav_module
    with patch.object(nav_module, "go2", mock_go2), \
         patch.object(nav_module, "spatial_memory", mock_memory):
        nav = nav_module.Navigator()
        nav.target = "窗边"
        state = nav.state
    assert state["target"] == "窗边"
    assert "mode" in state
