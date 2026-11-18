"""Stand commands need observed posture, not just transport acceptance."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from cloud.go2.connection.webrtc import Go2Connection


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "initial", "height"),
                         [("StandDown", "standing", .07), ("StandUp", "lying", .32)])
async def test_stand_command_does_not_use_height_without_joint_and_imu_feedback(command, initial, height):
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body(initial, confirmation="observed")
    async def publish(_topic, _options):
        return {"data": {"header": {"status": {"code": 0}}}}
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(
        pub_sub=SimpleNamespace(publish_request_new=publish)))
    result = await robot.send_command(command)
    assert result["result"] == "accepted"
    assert result["completion_supported"] is False
    for _ in range(8):
        robot._on_state({"data": {"body_height": height, "velocity": [0, 0, 0]}})
    assert robot.body_state is None
    assert robot.pending_action is not None


@pytest.mark.asyncio
async def test_stand_command_without_posture_feedback_remains_unconfirmed():
    async def publish(_topic, _options):
        return None
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body("standing", confirmation="observed")
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(
        pub_sub=SimpleNamespace(publish_request_new=publish)))
    result = await robot.send_command("StandDown")
    assert result["result"] == "accepted"
    robot._expire_body_action(robot.pending_action["request_id"])
    assert robot.pending_action["phase"] == "timeout"
    assert robot.runtime.data['last_action']['error'] == 'completion_not_observed'
    assert robot.body_state is None


@pytest.mark.asyncio
async def test_stale_robot_telemetry_marks_device_offline():
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body("standing", confirmation="observed")
    robot._last_state_received_at = time.monotonic() - 30
    observed = []
    robot._peer_state_callback = lambda: observed.append(
        (robot.is_connected, robot.body_state))

    await asyncio.wait_for(robot._status_loop(), timeout=1)

    assert robot.is_connected is False
    assert robot.body_state is None
    assert observed[-1] == (False, None)
