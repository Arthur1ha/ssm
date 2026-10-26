"""Stand commands need observed posture, not just transport acceptance."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cloud.go2.connection.webrtc import Go2Connection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "initial", "before", "after", "expected"),
    [
        ("StandDown", "standing", 0.32, 0.07, "lying"),
        ("StandUp", "lying", 0.07, 0.32, "standing"),
    ],
)
async def test_stand_command_waits_for_fresh_stable_height(
    command, initial, before, after, expected,
):
    sent = asyncio.Event()

    async def publish(_topic, _options):
        sent.set()

    robot = Go2Connection()
    robot.is_connected = True
    robot.fsm_state = initial
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(
        pub_sub=SimpleNamespace(publish_request_new=publish)))

    pending = asyncio.create_task(robot.send_command(command, completion_timeout=1))
    await sent.wait()
    await asyncio.sleep(0)
    robot._on_state({"data": {"body_height": before, "progress": 0}})
    assert not pending.done()
    for _ in range(2):
        robot._on_state({"data": {"body_height": after, "progress": 0}})
    assert not pending.done()
    robot._on_state({"data": {"body_height": after, "progress": 0}})
    assert await pending == {"result": "ok", "confirmation": "body_height",
                             "fsm_state": expected, "body_height": after}


@pytest.mark.asyncio
async def test_stand_command_without_posture_feedback_remains_unconfirmed():
    async def publish(_topic, _options):
        return None

    robot = Go2Connection()
    robot.is_connected = True
    robot.fsm_state = "standing"
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(
        pub_sub=SimpleNamespace(publish_request_new=publish)))

    result = await robot.send_command("StandDown", completion_timeout=0.01)
    assert result["result"] == "timeout"
    assert result["error"] == "completion_not_observed"


@pytest.mark.asyncio
async def test_stale_robot_telemetry_marks_device_offline():
    robot = Go2Connection()
    robot.is_connected = True
    robot.fsm_state = "standing"
    robot._last_state_received_at = time.monotonic() - 30
    observed = []
    robot._peer_state_callback = lambda: observed.append(
        (robot.is_connected, robot.fsm_state))

    await asyncio.wait_for(robot._status_loop(), timeout=1)

    assert robot.is_connected is False
    assert robot.fsm_state == "offline"
    assert observed[-1] == (False, "offline")
