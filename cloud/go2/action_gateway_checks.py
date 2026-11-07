"""Action catalog integration checks, runnable without a robot or remote model.

Run explicitly: uv run python -m pytest cloud/go2/action_gateway_checks.py
"""

from importlib import import_module
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from cloud.go2.actions import ACTIONS, BODY_STATES, SPORT_COMMANDS
from cloud.go2.connection.webrtc import Go2Connection
from cloud.go2.control import control_lease
from cloud.go2.execution import ExecutionContext


NEW_COMMANDS = ("Sit", "RiseSit", "Content", "FingerHeart", "FrontPounce", "FrontJump")
VISITOR = ExecutionContext("action-check-a", "visitor", False)
OTHER_VISITOR = ExecutionContext("action-check-b", "visitor", False)


@pytest.fixture
def robot(monkeypatch, tmp_path):
    # Legacy Card checks temporarily replace sys.modules['cloud.go2.router'].
    # Resolve the active module rather than the package's stale cached attribute.
    router = import_module("cloud.go2.router")
    from cloud.go2.tools import executor, tools
    from cloud.go2 import service
    from cloud.api import policy

    owner = control_lease.snapshot()
    if owner is not None:
        control_lease.release(owner.owner_session_id)
    connection = Go2Connection()
    connection.is_connected = True
    connection.runtime.body("standing", confirmation="observed")
    connection._sport_state_observed_at = time.monotonic()
    connection.send_command = AsyncMock(return_value={
        "result": "accepted", "confirmation": "sport_rpc", "confirmation_scope": "request",
        "request_status_code": 0, "fsm_state": None,
        "pending_action": {"command": "Sit", "phase": "accepted"},
    })
    monkeypatch.setattr(router, "go2", connection)
    monkeypatch.setattr(service, "go2", connection)
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setattr(executor, "go2", connection)
    monkeypatch.setattr(tools, "go2", connection)
    monkeypatch.setattr(policy, "_AUDIT_FILE", tmp_path / "audit.jsonl")
    yield connection
    owner = control_lease.snapshot()
    if owner is not None:
        control_lease.release(owner.owner_session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", NEW_COMMANDS)
async def test_new_actions_use_tool_boundary_and_preserve_confirmation(robot, command):
    from cloud.go2.tools.executor import execute_tool

    robot.runtime.body(ACTIONS[command].source_states[0], confirmation="observed")
    result = await execute_tool("go2_sport", {"cmd": command}, context=VISITOR)
    assert result["result"] == "accepted"
    assert result["action"] == command
    assert result["confirmation_scope"] == "request"
    assert result["request_status_code"] == 0
    assert result["pending_action"]["phase"] == "accepted"
    robot.send_command.assert_awaited_once_with(command)
    assert control_lease.snapshot().owner_session_id == VISITOR.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("command", NEW_COMMANDS)
async def test_new_actions_keep_existing_visitor_lease_conflicts(robot, command):
    from cloud.go2.tools.executor import execute_tool

    robot.runtime.body(ACTIONS[command].source_states[0], confirmation="observed")
    control_lease.acquire(OTHER_VISITOR)
    result = await execute_tool("go2_sport", {"cmd": command}, context=VISITOR)
    assert result["result"] == "blocked"
    assert result["error"] == "control_lease_conflict"
    robot.send_command.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,command", [("lying", "Content"), ("sitting", "FrontJump"),
                                         ("standing", "RiseSit"), (None, "Sit")])
async def test_illegal_body_transition_is_rejected_before_tool_dispatch(robot, state, command):
    from cloud.go2.tools.executor import execute_tool

    robot.runtime.body(state, confirmation="unknown" if state is None else "observed")
    result = await execute_tool("go2_sport", {"cmd": command}, context=VISITOR)
    assert result["result"] == "blocked"
    robot.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_actions_do_not_bypass_tool_parameter_or_role_checks(robot):
    from cloud.go2.tools.executor import execute_tool

    result = await execute_tool("go2_sport", {"cmd": "Sit", "force": True}, context=VISITOR)
    assert result["error"] == "invalid_params"
    result = await execute_tool("go2_sport", {"cmd": "Sit"}, context=ExecutionContext(
        "untrusted-agent", "agent", False))
    assert result["error"] == "admin_only_tool_denied"
    robot.send_command.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", NEW_COMMANDS)
async def test_direct_http_command_accepts_only_catalog_params(robot, command):
    from cloud.go2.router import CommandRequest, go2_command

    robot.runtime.body(ACTIONS[command].source_states[0], confirmation="observed")
    with pytest.raises(HTTPException) as error:
        await go2_command(CommandRequest(cmd=command, params={"force": True}), context=VISITOR)
    assert error.value.status_code == 400
    robot.send_command.assert_not_called()
    result = await go2_command(CommandRequest(cmd=command), context=VISITOR)
    assert result["confirmation_scope"] == "request"
    robot.send_command.assert_awaited_once_with(command)


@pytest.mark.asyncio
async def test_library_commands_outside_selected_catalog_are_not_public(robot):
    from cloud.go2.router import CommandRequest, go2_command

    with pytest.raises(HTTPException) as error:
        await go2_command(CommandRequest(cmd="FrontFlip"), context=VISITOR)
    assert error.value.status_code == 400
    robot.send_command.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", NEW_COMMANDS)
async def test_ordinary_stop_cannot_claim_interruption_of_new_actions(robot, command):
    from cloud.go2.tools.executor import execute_tool
    from cloud.go2.router import CommandRequest, go2_command

    robot.runtime.body(None, confirmation="unknown")
    robot.pending_action = {"command": command, "phase": "accepted"}
    result = await execute_tool("go2_sport", {"cmd": "StopMove"}, context=VISITOR)
    assert result["result"] == "blocked"
    assert "action_interrupt_not_confirmed" in result["message"]
    with pytest.raises(HTTPException) as error:
        await go2_command(CommandRequest(cmd="StopMove"), context=VISITOR)
    assert error.value.status_code == 400
    robot.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_emergency_stop_remains_reachable_with_uninterruptible_pending_action(robot, monkeypatch):
    router = import_module("cloud.go2.router")
    from cloud.go2.navigation.navigator import navigator

    robot.runtime.body(None, confirmation="unknown")
    robot.pending_action = {"command": "FrontJump", "phase": "accepted"}
    robot.stop_velocity = MagicMock()
    monkeypatch.setattr(navigator, "stop", MagicMock())
    monkeypatch.setattr(router, "_configure_vision_consumers", MagicMock())
    result = await router.emergency_stop(context=VISITOR)
    assert result["ok"] is True
    robot.stop_velocity.assert_called_once_with()
    assert robot.fsm_state is None
    assert robot.pending_action["command"] == "FrontJump"


@pytest.mark.asyncio
async def test_joystick_zero_keeps_stop_wire_params_empty(robot):
    from cloud.go2.router import VelocityRequest, go2_velocity

    robot.runtime.body("moving", confirmation="observed")
    robot.move_velocity = MagicMock(return_value={"result": "accepted"})
    result = await go2_velocity(VelocityRequest(vx=0, vy=0, vyaw=0), context=VISITOR)
    assert result["result"] == "accepted"
    robot.move_velocity.assert_called_once_with(0, 0, 0, deadman_s=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", NEW_COMMANDS)
async def test_gateway_forwards_new_command_with_original_identity(monkeypatch, command):
    from cloud.api import intents

    session = {"actor": "visitor", "session_id": "trusted-action-session"}
    dispatch = AsyncMock(return_value={"result": "accepted", "confirmation_scope": "request"})
    monkeypatch.setattr(intents, "_internal_request", dispatch)
    result = await intents._dispatch_go2(command, {}, session)
    assert result["result"] == "accepted"
    dispatch.assert_awaited_once_with(
        "/api/go2/commands", "POST", session, {"action": command, "params": {}})


def test_card_capabilities_and_topology_derive_from_same_catalog():
    from cloud.go2.card import _go2_state_machine, get_card_skill
    from cloud.go2.tools.registry import get_tool_spec

    schema = get_card_skill("go2_sport")["params_schema"]
    assert schema == get_tool_spec("go2_sport").params_schema
    assert set(schema["properties"]["cmd"]["enum"]) == set(SPORT_COMMANDS)
    graph = _go2_state_machine()
    assert set(graph["states"]) == set(BODY_STATES)
    for command in NEW_COMMANDS:
        action = ACTIONS[command]
        transition = next(t for t in graph["transitions"] if t["trigger"] == command)
        assert transition["src"] == action.source_states[0]
        assert transition["dst"] == (action.execution_state or action.expected_state)
        assert transition["label"] == action.label
        assert transition["dst"] in graph["views"]
    # No static edge promises that an unverified acrobatic action can be stopped
    # into a known standing posture. The safety endpoint remains reachable.
    for state in ("content", "finger_heart", "pouncing", "jumping"):
        assert not any(t["src"] == state and t["trigger"] == "StopMove"
                       for t in graph["transitions"])
    stops = {t["src"]: t for t in graph["transitions"] if t.get("safety")}
    assert stops["sitting"]["dst"] == "sitting"
    assert stops["jumping"]["dst"] == "jumping"
    assert "unknown" not in stops
    assert "unknown" not in graph["views"]
    assert all(edge["src"] != "unknown" and edge["dst"] != "unknown" for edge in graph["transitions"])
