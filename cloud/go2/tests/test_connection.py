import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from cloud.go2.connection import Go2Connection


def test_initial_state():
    conn = Go2Connection()
    assert conn.is_connected is False
    assert conn._latest_frame is None
    assert conn._robot_state == {}


def test_send_command_raises_when_not_connected():
    conn = Go2Connection()
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(conn.send_command("StandUp"))


def test_send_command_raises_for_unknown_command():
    conn = Go2Connection()
    conn.is_connected = True
    conn._conn = MagicMock()
    # ValueError must be raised before _conn is accessed
    with pytest.raises(ValueError, match="Unknown command"):
        asyncio.run(conn.send_command("FlyToMoon"))


def test_on_state_updates_robot_state_and_notifies_queues():
    conn = Go2Connection()
    q = asyncio.Queue(maxsize=10)
    conn._state_queues.append(q)

    msg = {"data": {"data": {"mode": 1, "body_height": 0.32, "velocity": [0.1, 0.0, 0.0]}}}
    conn._on_state(msg)

    assert conn._robot_state["mode"] == 1
    assert conn._robot_state["body_height"] == 0.32
    assert q.qsize() == 1


def test_new_and_remove_state_queue():
    conn = Go2Connection()
    q = conn.new_state_queue()
    assert q in conn._state_queues
    conn.remove_state_queue(q)
    assert q not in conn._state_queues


def test_remove_nonexistent_queue_does_not_raise():
    conn = Go2Connection()
    q = asyncio.Queue()
    conn.remove_state_queue(q)  # should not raise


def test_latest_frame_b64_returns_none_when_no_frame():
    conn = Go2Connection()
    assert conn.latest_frame_b64() is None


def test_latest_frame_b64_returns_base64_string():
    import base64
    conn = Go2Connection()
    conn._latest_frame = b"\xff\xd8\xff"   # fake JPEG header bytes
    result = conn.latest_frame_b64()
    assert result == base64.b64encode(b"\xff\xd8\xff").decode()
    assert isinstance(result, str)


def test_initial_odom_and_lowstate():
    conn = Go2Connection()
    assert conn.odom == {}
    assert conn.low_state == {}


def test_on_odom_parses_position_and_heading():
    import math
    conn = Go2Connection()
    msg = {
        "data": {
            "header": {"stamp": {"sec": 1000, "nanosec": 0}, "frame_id": "odom"},
            "pose": {
                "position": {"x": 1.0, "y": 2.0, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.7071, "w": 0.7071},
            },
        }
    }
    conn._on_odom(msg)
    assert conn.odom["x"] == pytest.approx(1.0)
    assert conn.odom["y"] == pytest.approx(2.0)
    assert conn.odom["heading"] == pytest.approx(math.pi / 2, abs=0.01)


def test_on_low_state_parses_battery_and_imu():
    conn = Go2Connection()
    msg = {
        "data": {
            "bms_state": {"soc": 75},
            "power_v": 28.3,
            "imu_state": {"rpy": [0.01, -0.01, 1.57]},
            "foot_force": [90, 85, 80, 88],
        }
    }
    conn._on_low_state(msg)
    assert conn.low_state["battery_soc"] == 75
    assert conn.low_state["power_v"] == pytest.approx(28.3)
    assert conn.low_state["imu_rpy"][2] == pytest.approx(1.57)
    assert conn.low_state["foot_force"] == [90, 85, 80, 88]


def test_odom_queue_receives_updates():
    conn = Go2Connection()
    q = conn.new_odom_queue()
    msg = {
        "data": {
            "header": {"stamp": {"sec": 1, "nanosec": 0}, "frame_id": "odom"},
            "pose": {
                "position": {"x": 3.0, "y": 4.0, "z": 0.0},
                "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
            },
        }
    }
    conn._on_odom(msg)
    assert q.qsize() == 1
    data = q.get_nowait()
    assert data["x"] == pytest.approx(3.0)
    conn.remove_odom_queue(q)
    assert q not in conn._odom_queues
