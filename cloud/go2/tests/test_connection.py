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
