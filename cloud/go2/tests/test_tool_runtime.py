"""Go2 tool boundary regression tests; no robot or remote model is used."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2 import agent
from cloud.go2.agentcore.tools.executor import execute_tool
from cloud.go2.agentcore.tools.registry import TOOL_REGISTRY
from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.execution import ExecutionContext


ADMIN = ExecutionContext("runtime-admin", "admin", True)
VISITOR = ExecutionContext("runtime-visitor", "visitor", False)


def _state(**changes):
    state = {
        "session_id": "runtime-admin", "user_msg": "站起来", "skill_id": "",
        "params": {}, "memory_context": "", "planned_tools": [], "tool_results": [],
        "response_text": "", "early_exit": False, "llm_available": True,
        "error": "", "actor": "admin", "persist": True,
    }
    state.update(changes)
    return state


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
async def test_accepted_motion_blocks_next_motion(robot):
    state = _state(planned_tools=[
        {"tool": "go2_sport", "params": {"cmd": "Hello"}},
        {"tool": "go2_sport", "params": {"cmd": "Dance1"}},
    ])
    results = [item async for item in agent._execute_calls(state, ADMIN)]
    assert [item["result"]["result"] for item in results] == ["accepted", "blocked"]
    robot.assert_awaited_once_with("Hello")


@pytest.mark.asyncio
async def test_peer_request_waits_for_final_ok_before_dependent_action(monkeypatch, robot):
    from cloud.go2.agentcore.tools import tools as tool_impl

    async def peer_result(*_args, **_kwargs):
        return {"result": "timeout", "request_id": "peer-1"}

    monkeypatch.setitem(tool_impl.TOOL_FN_MAP, "request_peer_skill", peer_result)
    state = _state(planned_tools=[
        {"tool": "request_peer_skill", "params": {
            "target_unit_id": "esp32_desk_led", "skill_id": "set_state", "params": {"state": "ON"},
        }},
        {"tool": "go2_sport", "params": {"cmd": "Hello"}},
    ])
    results = [item async for item in agent._execute_calls(state, ADMIN)]
    assert [item["result"]["result"] for item in results] == ["timeout"]
    robot.assert_not_called()


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
async def test_invalid_planner_json_is_error_not_empty_plan(monkeypatch, robot):
    model = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content="not json")))
    monkeypatch.setattr(agent, "get_text_llm", lambda: model)
    state = await agent.planner_node(_state(actor="visitor", persist=False))
    assert state["error"] == "invalid_planner_json"
    assert state["early_exit"] is True


@pytest.mark.asyncio
async def test_single_peer_tool_object_is_planned_without_dispatch(monkeypatch, robot):
    call = ('{"tool":"request_peer_skill","params":{'
            '"target_unit_id":"esp32_desk_led","skill_id":"set_light_state",'
            '"params":{"state":"OFF"}}}')
    model = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content=call)))
    monkeypatch.setattr(agent, "get_text_llm", lambda: model)
    state = await agent.planner_node(_state(user_msg="关掉灯"))
    assert state["planned_tools"] == [{
        "tool": "request_peer_skill",
        "params": {"target_unit_id": "esp32_desk_led", "skill_id": "set_light_state",
                   "params": {"state": "OFF"}},
    }]
    robot.assert_not_called()


@pytest.mark.asyncio
async def test_peer_params_array_is_wrapped_as_tool_call(monkeypatch, robot):
    # This is the shape the live model returned for "帮我把灯打开".
    content = ('[{"target_unit_id":"esp32_desk_led","skill_id":"set_light_state",'
               '"params":{"state":"ON"},"request_id":"open_light_20260711_001"}]')
    model = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content=content)))
    monkeypatch.setattr(agent, "get_text_llm", lambda: model)
    state = await agent.planner_node(_state(user_msg="帮我把灯打开"))
    assert state["planned_tools"] == [{"tool": "request_peer_skill", "params": {
        "target_unit_id": "esp32_desk_led", "skill_id": "set_light_state",
        "params": {"state": "ON"}, "request_id": "open_light_20260711_001",
    }}]
    robot.assert_not_called()


@pytest.mark.asyncio
async def test_peer_reply_prompt_uses_personality_and_confirmed_result(monkeypatch):
    result = [{"tool": "request_peer_skill", "result": {
        "result": "ok", "request_id": "req_internal_1",
        "target_unit_id": "esp32_desk_led", "task_id": "req_internal_1",
    }}]
    model = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content="汪，灯已经弄好啦。")))
    monkeypatch.setattr(agent, "get_text_llm", lambda: model)
    monkeypatch.setattr(agent, "get_system_prompt", lambda: "好奇、活泼的小狗")
    reply, available = await agent._generate_response("帮我把灯打开", "", result)
    messages = model.ainvoke.await_args.args[0]
    assert available and reply == "汪，灯已经弄好啦。"
    assert "好奇、活泼的小狗" in messages[1].content
    assert '"overall_result": "ok"' in messages[1].content
    assert "req_internal_1" not in messages[1].content
    assert "esp32_desk_led" not in messages[1].content
    assert "不要把 ok 说成尚未确认" in messages[0].content


@pytest.mark.asyncio
async def test_peer_reply_does_not_repeat_unconfirmed_claim_or_internal_id(monkeypatch):
    result = [{"tool": "request_peer_skill", "result": {"result": "ok"}}]
    model = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(
        content="请求编号 req_internal_1 已发送，但还没得到完成确认。")))
    monkeypatch.setattr(agent, "get_text_llm", lambda: model)
    reply, available = await agent._generate_response("把灯打开", "", result)
    assert available and reply == "汪，交给我的事已经办妥啦。"


@pytest.mark.asyncio
async def test_tool_call_limit_is_enforced(monkeypatch, robot):
    calls = '[{"tool":"go2_status"},{"tool":"go2_status"},{"tool":"go2_status"},{"tool":"go2_status"}]'
    model = MagicMock(ainvoke=AsyncMock(return_value=MagicMock(content=calls)))
    monkeypatch.setattr(agent, "get_text_llm", lambda: model)
    state = await agent.planner_node(_state(actor="visitor", persist=False))
    assert state["error"] == "tool_call_limit_exceeded"


@pytest.mark.asyncio
async def test_unknown_skill_and_ambiguous_steps_never_move(robot):
    unknown = await agent.planner_node(_state(skill_id="go2_fly"))
    assert unknown["error"] == "unknown_skill"
    unclear = await agent.planner_node(_state(user_msg="移动两步", session_id="steps-session"))
    assert unclear["result_override"] == "needs_clarification"
    assert "duration" in unclear["missing_fields"]
    robot.assert_not_called()


def test_clarification_requires_both_fields_and_resumes_goal():
    first = _state(user_msg="移动两步", session_id="clarify-session")
    assert agent._clarification(first)["missing_fields"] == ["direction", "duration"]
    duration = _state(user_msg="2秒", session_id="clarify-session")
    assert agent._clarification(duration)["missing_fields"] == ["direction"]
    direction = _state(user_msg="向前", session_id="clarify-session")
    assert agent._clarification(direction) is None
    assert direction["user_msg"].startswith("向前移动2秒")


@pytest.mark.asyncio
async def test_sse_and_standard_chat_report_same_accepted_status(monkeypatch, robot):
    async def planned(state):
        return {**state, "planned_tools": [{"tool": "go2_sport", "params": {"cmd": "Hello"}}],
                "early_exit": False}
    monkeypatch.setattr(agent, "planner_node", planned)
    context = ExecutionContext("same-status", "visitor", False)
    normal = await agent.run_agent("same-status", "打招呼", context=context)
    events = [event async for event in agent.run_agent_stream("same-status", "打招呼", context=context)]
    assert normal["result"] == "accepted"
    assert normal["actions_taken"] == []
    assert next(event for event in events if event["type"] == "tool_done")["result"]["result"] == "accepted"
    assert events[-1]["result"] == "accepted"
    assert robot.await_count == 2


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
