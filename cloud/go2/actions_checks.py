"""Body-action contract regressions; no robot, network, or model calls.

Explicitly run with ``uv run python -m pytest cloud/go2/actions_checks.py``.
"""

import asyncio
from types import SimpleNamespace

import pytest

from cloud.go2.actions import ACTIONS, BODY_STATES, SPORT_COMMANDS
from cloud.go2.connection.fsm import CommandBlocked, Go2FSM
from cloud.go2.connection.webrtc import Go2Connection


NEW_ACTIONS = (
    ("Sit", "standing", 1009, "sitting"),
    ("RiseSit", "sitting", 1010, "standing"),
    ("Content", "standing", 1020, "standing"),
    ("FingerHeart", "standing", 1036, "standing"),
    ("FrontPounce", "standing", 1032, "standing"),
    ("FrontJump", "standing", 1031, "standing"),
)


def robot_at(state="standing", code=0):
    sent = []

    async def publish(topic, options):
        sent.append((topic, options))
        return {"data": {"header": {"status": {"code": code}}}}

    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body(state, confirmation='observed')
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=publish, publish_without_callback=lambda *a, **k: None)))
    return robot, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("command,source,api_id,target", NEW_ACTIONS)
async def test_new_actions_use_declared_sport_mapping_without_claiming_completion(command, source, api_id, target):
    robot, sent = robot_at(source)
    result = await robot.send_command(command)
    assert sent[0][1]['api_id'] == api_id
    assert type(sent[0][1]['id']) is int
    assert result["result"] == "accepted"
    assert result["confirmation_scope"] == "request"
    assert result["completion_supported"] is False
    assert result["target_state"] == target
    assert robot.body_state is None
    assert robot.pending_action["command"] == command
    assert robot.available_actions == []
    # The recorded heights overlap sitting and standing. Neither height nor an
    # uncorrelated progress cycle may unlock the next action.
    for height, progress in ((0.07, 0), (0.32, 0.5), (0.32, 0), (0.32, 0)):
        robot._on_state({"data": {"body_height": height, "progress": progress}})
    assert robot.body_state is None
    with pytest.raises(CommandBlocked, match="unconfirmed"):
        await robot.send_command("Hello")
    with pytest.raises(CommandBlocked, match="unconfirmed"):
        robot.move_velocity(0.1, 0, 0)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_sit_does_not_unlock_rise_sit_without_a_real_sitting_observation():
    robot, sent = robot_at()
    await robot.send_command("Sit")
    with pytest.raises(CommandBlocked, match="unconfirmed"):
        await robot.send_command("RiseSit")
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,command", [("lying", "Sit"), ("sitting", "Dance1"),
                                            ("sitting", "Move"), (None, "FrontJump")])
async def test_illegal_sources_do_not_publish(source, command):
    robot, sent = robot_at(source)
    with pytest.raises(CommandBlocked):
        await robot.send_command(command, {"x": 0.1} if command == "Move" else None)
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize("command,source,api_id,target", NEW_ACTIONS)
async def test_device_rejection_cannot_become_success_or_a_target_pose(command, source, api_id, target):
    robot, sent = robot_at(source, 3203)
    result = await robot.send_command(command)
    assert result["result"] == "error"
    assert result["request_status_code"] == 3203
    assert robot.body_state == source
    assert robot.pending_action is None
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_stop_request_does_not_resolve_unconfirmed_jump_or_invent_standing():
    robot, sent = robot_at()
    await robot.send_command("FrontJump")
    request_id = robot.pending_action["request_id"]
    stopped = await robot.send_command("StopMove")
    assert stopped["result"] == "accepted"
    assert robot.body_state is None
    assert robot.pending_action["request_id"] == request_id
    assert robot.pending_action["phase"] == "stop_requested_unconfirmed"
    robot.stop_velocity()
    assert robot.body_state is None
    with pytest.raises(CommandBlocked):
        await robot.send_command("Hello")
    assert len(sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sitting", "lying"])
async def test_emergency_zero_velocity_preserves_stable_pose(source):
    robot, sent = robot_at(source)
    assert robot.stop_velocity()["body_state"] == source
    assert robot.body_state == source


@pytest.mark.asyncio
async def test_unknown_parameters_and_library_commands_are_rejected_before_publish():
    robot, sent = robot_at()
    with pytest.raises(ValueError, match="parameters"):
        await robot.send_command("Sit", {"data": True})
    with pytest.raises(ValueError, match="Unknown command"):
        await robot.send_command("FrontFlip")
    assert not sent


@pytest.mark.asyncio
@pytest.mark.parametrize("command,initial,height,target", [
    ("StandDown", "standing", 0.07, "lying"), ("StandUp", "lying", 0.32, "standing")])
async def test_height_only_feedback_does_not_confirm_stand_commands(command, initial, height, target):
    robot, sent = robot_at(initial)
    result = await robot.send_command(command)
    assert result["result"] == "accepted"
    assert result["completion_supported"] is False
    for _ in range(8):
        robot._on_state({"data": {"body_height": height, "velocity": [0, 0, 0]}})
    assert robot.body_state is None
    assert robot.pending_action["command"] == command
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_posture_timeout_cannot_release_pending_motion_from_height_alone():
    robot, sent = robot_at()
    result = await robot.send_command("StandDown")
    assert result["result"] == "accepted"
    robot._expire_body_action(robot.pending_action["request_id"])
    assert robot.runtime.data["last_action"]["result"] == "timeout"
    assert robot.body_state is None
    assert robot.pending_action["phase"] == "timeout"
    for _ in range(3):
        robot._on_state({"data": {"body_height": 0.32, "progress": 0}})
    assert robot.body_state is None
    with pytest.raises(CommandBlocked):
        await robot.send_command("Hello")
    assert len(sent) == 1


def test_uncorrelated_progress_never_completes_old_or_new_gestures():
    robot = Go2Connection()
    robot.is_connected = True
    for state in ("greeting", "stretching", "dancing1", "dancing2", "jumping"):
        robot.body_state = state
        robot._on_state({"data": {"progress": 0.8}})
        robot._on_state({"data": {"progress": 0}})
        assert robot.body_state == state


def test_catalog_contains_only_requested_public_expansion():
    assert len(BODY_STATES) == 12
    assert set(SPORT_COMMANDS) == {"StandUp", "StandDown", "StopMove", "Hello", "Stretch",
                                   "Dance1", "Dance2", *(row[0] for row in NEW_ACTIONS)}
    assert not any(ACTIONS[row[0]].interruptible for row in NEW_ACTIONS)


@pytest.mark.asyncio
async def test_old_request_failure_after_reconnect_cannot_mutate_new_connection():
    robot, _ = robot_at()
    started = asyncio.Event()
    fail_old = asyncio.Event()

    async def late_failure(topic, options):
        started.set()
        await fail_old.wait()
        raise TimeoutError("old connection request failed late")

    robot._conn.datachannel.pub_sub.publish_request_new = late_failure
    old_task = asyncio.create_task(robot.send_command("Sit"))
    await started.wait()
    await robot.disconnect()
    new_connection = SimpleNamespace()
    robot._conn = new_connection
    robot.is_connected = True
    robot.body_state = None
    new_pending = {"command": "RiseSit", "request_id": "new-connection-action", "phase": "accepted"}
    robot.pending_action = new_pending
    fail_old.set()
    result = await old_task
    assert result["result"] == "cancelled"
    assert robot.is_connected is True
    assert robot._conn is new_connection
    assert robot.body_state is None
    assert robot.pending_action == new_pending


@pytest.mark.asyncio
async def test_cancelled_posture_wait_does_not_accept_height_only_recovery():
    robot, sent = robot_at()
    # Explicitly supported joint-feedback capability creates a physical waiter.
    # The cancellation regression then withholds LOW/IMU observations.
    robot._joint_observation_available = True
    import time
    robot._last_motor_observed_at = time.monotonic()
    task = asyncio.create_task(robot.send_command("StandDown"))
    for _ in range(8):
        await asyncio.sleep(0)
        if robot._completion is not None:
            break
    assert robot._completion is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert robot.pending_action["phase"] == "cancelled_unconfirmed"
    for _ in range(3):
        robot._on_state({"data": {"body_height": 0.07}})
    assert robot.body_state is None
    assert robot._completion is None
    assert len(sent) == 1
