"""Go2 tool boundary regression tests; no robot or remote model is used."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2.agentcore.tools.executor import execute_tool
from cloud.go2.agentcore.tools.registry import TOOL_REGISTRY
from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.execution import ExecutionContext


ADMIN = ExecutionContext("runtime-admin", "admin", True)
VISITOR = ExecutionContext("runtime-visitor", "visitor", False)




@pytest.fixture
def robot(monkeypatch):
    owner = control_lease.snapshot()
    if owner is not None:
        control_lease.release(owner.owner_session_id)
    monkeypatch.setattr(go2, "is_connected", True)
    monkeypatch.setattr(go2, "fsm_state", "standing")
    send = AsyncMock(return_value={"result": "accepted"})
    monkeypatch.setattr(go2, "send_command", send)
    return send


@pytest.mark.asyncio
async def test_registry_covers_all_implementations():
    from cloud.go2.agentcore.tools.tools import TOOL_FN_MAP
    assert set(TOOL_REGISTRY) == set(TOOL_FN_MAP)


@pytest.mark.asyncio
async def test_invalid_motion_params_never_send(robot):
    result = await execute_tool("go2_move", {"direction": "forward", "speed": float("nan")}, context=ADMIN)
    assert result["error"] == "invalid_params"
    result = await execute_tool("go2_move", {"direction": "forward", "speed": 0.5001}, context=ADMIN)
    assert result["error"] == "invalid_params"
    robot.assert_not_called()


@pytest.mark.asyncio
async def test_motion_speed_boundary_is_consistent(monkeypatch, robot):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    result = await execute_tool("go2_move", {"direction": "forward", "speed": 0.5}, context=ADMIN)
    assert result["result"] == "accepted"
    assert robot.await_args_list[0].args[1]["x"] == 0.5


@pytest.mark.asyncio
async def test_visitor_persistent_tool_denied_before_write(robot):
    result = await execute_tool("go2_add_rule", {"trigger": "人", "action": "Hello"}, context=VISITOR)
    assert result["error"] == "visitor_persistence_denied"


@pytest.mark.asyncio
async def test_sport_accepted_is_not_completion(robot):
    result = await execute_tool("go2_sport", {"cmd": "Hello"}, context=ADMIN)
    assert result["result"] == "accepted"
    robot.assert_awaited_once_with("Hello")






@pytest.mark.asyncio
async def test_peer_tool_rejects_non_object_params_before_dispatch(robot):
    result = await execute_tool("request_peer_skill", {
        "target_unit_id": "esp32_desk_led", "skill_id": "set_state", "params": "ON",
    }, context=VISITOR)
    assert result == {"result": "rejected", "error": "invalid_params", "message": "params must be object"}


@pytest.mark.asyncio
async def test_peer_tool_uses_trusted_go2_context(monkeypatch, robot):
    from cloud.peers import gateway
    from cloud.go2.agentcore.tools import tools as tool_impl

    calls = []

    def fake_request(**kwargs):
        calls.append(kwargs)
        return {"result": "ok", "request_id": "peer-test"}

    monkeypatch.setattr(gateway, "request_peer_skill", fake_request)
    async def inline_call(fn, **kwargs):
        return fn(**kwargs)
    monkeypatch.setattr(tool_impl.asyncio, "to_thread", inline_call)
    delegated = ExecutionContext("visitor-session", "visitor", False,
                                 origin_kind="user_delegated", expires_at=int(time.time()) + 60)
    result = await execute_tool("request_peer_skill", {
        "target_unit_id": "esp32_desk_led", "skill_id": "set_state", "params": {"state": "ON"},
        "request_id": "same-request",
    }, context=delegated)
    assert result["result"] == "ok"
    assert calls[0]["initiator_unit_id"] == "go2"
    assert calls[0]["context"] is delegated
    assert calls[0]["origin_kind"] == "user_delegated"
    assert calls[0]["request_id"] == "same-request"


def test_direct_user_takeover_cancels_only_go2_initiated_queue(monkeypatch):
    from cloud.go2 import router
    from cloud.peers import gateway

    calls = []
    monkeypatch.setattr(gateway, "cancel_pending", lambda **kwargs: calls.append(kwargs))
    router._cancel_pending_peer_requests(ExecutionContext("user-1", "visitor", False))
    router._cancel_pending_peer_requests(ExecutionContext(
        "mode-1", "agent", False, origin_kind="agent_autonomous"
    ))
    router._cancel_pending_peer_requests(ExecutionContext(
        "system", "admin", True, origin_kind="system"
    ))
    assert calls == [{"initiator_unit_id": "go2", "origin_kind": "agent_autonomous",
                      "reason": "user_takeover"}]


@pytest.mark.asyncio
async def test_unknown_tool_is_structured_error(robot):
    result = await execute_tool("go2_fly", {}, context=ADMIN)
    assert result["error"] == "unknown_tool"
    robot.assert_not_called()




















@pytest.mark.asyncio
async def test_stop_failure_is_traceable(monkeypatch, robot):
    from cloud.go2.agentcore.tools.tools import go2_move
    from cloud.go2.execution import use_context

    robot.side_effect = [{"result": "accepted"}, {"result": "timeout"}]
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with use_context(ADMIN):
        result = await go2_move("forward", duration=1)
    assert result["result"] == "timeout"
    assert result["error"] == "stop_not_confirmed"
    assert result["task_id"]


@pytest.mark.asyncio
async def test_old_session_does_not_stop_new_lease_owner(monkeypatch, robot):
    from cloud.go2.agentcore.tools.tools import go2_move
    from cloud.go2.execution import use_context

    async def preempt_during_move(_duration):
        control_lease.acquire(ADMIN)

    monkeypatch.setattr(asyncio, "sleep", preempt_during_move)
    with use_context(VISITOR):
        result = await go2_move("forward", duration=1)
    assert result["error"] == "stop_not_confirmed"
    assert result["stop"]["error"] == "lease_transferred"
    robot.assert_awaited_once()
