"""Simulated mode execution boundaries; no robot or network access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud.go2.agentcore.mode import free_explore, reactive
from cloud.go2.execution import ExecutionContext


def test_reactive_action_uses_executor_and_stops_after_mode_switch(monkeypatch):
    calls = []

    async def fake_execute(name, params, *, context):
        calls.append((name, params, context))
        return {"result": "accepted", "message": "device acknowledgement only"}

    monkeypatch.setattr(reactive, "execute_tool", fake_execute)
    monkeypatch.setattr(reactive, "_publish_thought", lambda payload: None)
    monkeypatch.setattr(reactive.episode_memory, "add", lambda *args: (_ for _ in ()).throw(AssertionError("accepted is not complete")))
    mind = reactive.ReactiveMind()
    context = ExecutionContext("mode-owner", "admin", True)
    mind.set_active(True, context)
    asyncio.run(mind._dispatch_action("Hello", "test", "test"))
    assert calls == [("go2_sport", {"cmd": "Hello"}, context)]
    mind.set_active(False)
    asyncio.run(mind._dispatch_action("Hello", "test", "test"))
    assert len(calls) == 1


def test_reactive_prompt_records_version_and_memory_source(monkeypatch):
    response = SimpleNamespace(content='{"action": null, "reason": "静观"}')
    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
    monkeypatch.setattr(reactive, "get_text_llm", lambda: llm)
    monkeypatch.setattr(reactive, "_publish_thought", lambda payload: None)
    mind = reactive.ReactiveMind()
    mind.set_active(True)
    frame = {"persons": {"detected": False, "count": 0},
             "faces": {"detected": False, "count": 0}, "change_type": "none"}
    asyncio.run(mind._autonomous_reason(frame))
    metadata = mind._last_prompt_metadata
    assert metadata["task"] == "reactive"
    assert metadata["prompt_version"]
    assert metadata["memory_sources"] == ("episode",)
    assert "固定约束版本" in llm.ainvoke.await_args.args[0][0].content


def test_free_explore_motion_uses_executor_and_interrupt_gate(monkeypatch):
    calls = []

    async def fake_execute(name, params, *, context):
        calls.append((name, params))
        return {"result": "accepted"}

    monkeypatch.setattr(free_explore, "execute_tool", fake_execute)
    monkeypatch.setattr(free_explore.go2, "is_connected", True)
    drive = free_explore.Drive()
    result = asyncio.run(drive._execute_motion("go2_move", {"direction": "forward"}))
    assert result["result"] == "accepted"
    assert drive._last_execution_result == "accepted"
    drive.user_interrupt = True
    result = asyncio.run(drive._execute_motion("go2_sport", {"cmd": "Hello"}))
    assert result["result"] == "cancelled"
    assert calls == [("go2_move", {"direction": "forward"})]


def test_explore_blocked_direction_never_falls_back_to_forward_motion(monkeypatch):
    drive = free_explore.Drive()
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(
        odom={"x": 0.0, "y": 0.0, "heading": 0.0}, occupancy_grid=object(),
        is_connected=True,
    ))
    monkeypatch.setattr(free_explore.frontier_mod, "find_exploration_target", lambda *args: None)
    execute = AsyncMock(return_value={"result": "ok"})
    monkeypatch.setattr(free_explore, "execute_tool", execute)
    result = asyncio.run(drive._exec_explore_direction("left"))
    assert "路径不通" in result
    assert drive._last_execution_result == "blocked"
    execute.assert_not_awaited()
