"""Simulated mode execution boundaries; no robot or network access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cloud.go2.agentcore.mode import free_explore, reactive
from cloud.go2 import router as go2_router
from cloud.go2.execution import ExecutionContext


def test_reactive_action_uses_executor_and_stops_after_mode_switch(monkeypatch):
    calls = []

    async def fake_execute(name, params, *, context):
        calls.append((name, params, context))
        return {"result": "accepted", "message": "device acknowledgement only"}

    monkeypatch.setattr(reactive, "execute_tool", fake_execute)
    monkeypatch.setattr(reactive.episode_memory, "add", lambda *args: (_ for _ in ()).throw(AssertionError("accepted is not complete")))
    mind = reactive.ReactiveMind()
    context = ExecutionContext("mode-owner", "admin", True)
    mind.set_active(True, context)
    asyncio.run(mind._dispatch_action("Hello", "test", "test"))
    assert len(calls) == 1
    assert calls[0][:2] == ("go2_sport", {"cmd": "Hello"})
    assert calls[0][2].session_id == context.session_id
    assert calls[0][2].origin_kind == "agent_autonomous"
    mind.set_active(False)
    asyncio.run(mind._dispatch_action("Hello", "test", "test"))
    assert len(calls) == 1


def test_reactive_prompt_records_version_and_memory_source(monkeypatch):
    response = SimpleNamespace(content='{"action": null, "reason": "静观"}')
    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response))
    monkeypatch.setattr(reactive, "get_text_llm", lambda: llm)
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


def test_explore_vision_error_stops_before_motion_and_hides_provider_detail(monkeypatch):
    from cloud.space import activity as activity_module

    events = []
    store = SimpleNamespace(
        current=lambda: {"activity_id": "act_vision"},
        record_event=lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(activity_module, "get_activity_store", lambda: store)
    monkeypatch.setattr(free_explore, "go2_observe", AsyncMock(
        side_effect=RuntimeError("403 Free quota exhausted")))
    monkeypatch.setattr(free_explore, "get_text_llm", lambda: (_ for _ in ()).throw(
        AssertionError("exploration must stop before planning")))
    drive = free_explore.Drive()
    drive.owner_session_id = "act_vision"

    asyncio.run(drive._do_explore())

    assert drive._vision_retry_after > 0
    assert len(events) == 1
    assert events[0][0][1] == "message"
    assert "视觉服务暂不可用" in events[0][1]["text"]
    assert "403" not in events[0][1]["text"]


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


def test_activity_mode_ownership_and_stop_confirmation(monkeypatch):
    monkeypatch.setattr(go2_router.go2, "is_connected", True)
    monkeypatch.setattr(go2_router, "_autonomy_mode", "manual")
    monkeypatch.setattr(go2_router, "_activity_mode_owner", None)
    monkeypatch.setattr(go2_router, "_configure_vision_consumers", lambda *args, **kwargs: None)
    monkeypatch.setattr(go2_router.control_lease, "snapshot", lambda: None)
    released = []
    monkeypatch.setattr(go2_router.control_lease, "release", lambda owner: released.append(owner))
    monkeypatch.setattr(go2_router.go2, "send_command",
                        AsyncMock(return_value={"result": "accepted"}))

    async def scenario():
        started = await go2_router.set_activity_mode("free_explore", "act_first")
        assert started["ok"] and go2_router.get_activity_mode_owner() == "act_first"
        other = await go2_router.set_activity_mode("manual", "act_other")
        assert other["reason"] == "not_activity_owned"
        assert go2_router.get_activity_mode_owner() == "act_first"
        stopped = await go2_router.set_activity_mode("manual", "act_first")
        assert stopped["physical_stop"] == "unknown"
        assert go2_router.get_activity_mode_owner() is None
        assert released == []

    asyncio.run(scenario())


def test_explore_question_pauses_and_keeps_private_image_reference(tmp_path, monkeypatch):
    from cloud.go2 import activity_media
    from cloud.space import activity as activity_module

    events = []
    store = SimpleNamespace(
        current=lambda: {"activity_id": "act_question"},
        record_event=lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(activity_module, "get_activity_store", lambda: store)
    monkeypatch.setattr(activity_media, "_ROOT", tmp_path)
    monkeypatch.setattr(free_explore.go2, "is_connected", True)
    monkeypatch.setattr(free_explore.go2, "_latest_frame", b"\xff\xd8test\xff\xd9")
    monkeypatch.setattr(free_explore.episode_memory, "add", lambda *args: None)
    drive = free_explore.Drive()
    drive.owner_session_id = "act_question"
    result = asyncio.run(drive._exec_explore_tool("ask_user", {"question": "这个盒子是什么？"}))
    assert "暂停" in result
    assert drive._pending_question_id
    assert drive._last_execution_result == "paused"
    assert events[0][0][1] == "question"
    assert events[0][1]["media_ref"].startswith("/api/go2/activity-media/")
    assert activity_media.load_snapshot("act_question", drive._pending_question_id) == b"\xff\xd8test\xff\xd9"
