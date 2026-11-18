"""A color-hunt continuation cannot settle or publish into another run."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cloud.go2.mode import free_explore
from cloud.peers.games import color_hunt
from cloud.space.execution import ExecutionContext


class MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread_id):
        yield


@pytest.fixture
def hunt(monkeypatch):
    # Import the role graph before replacing its connected-device singleton.
    # Module initialization builds tool adapters from the real class contract.
    import cloud.agents.go2.graph

    drive, saver, events = free_explore.Drive(), MemorySaver(), []
    drive.owner_session_id, drive._generation = "act_A", 1
    drive._context = ExecutionContext("act_A", "visitor", False, origin_kind="agent_autonomous")
    drive._active_goal = {"event_id": "message_A", "activity_id": "act_A",
                          "generation": 1, "owner_session_id": "act_A"}
    current = ["act_A"]
    monkeypatch.setattr(drive, "_activity_id", lambda: current[0])
    monkeypatch.setattr(drive, "_activity_event", lambda *args, **kwargs:
        events.append((current[0], args, kwargs)) or {"event_id": "evt_observed", "ts": time.time()})
    robot = SimpleNamespace(is_connected=True, send_command=AsyncMock(return_value={"result": "ok"}))
    nav = SimpleNamespace(_snapshot=lambda: (({}, object()), None), stop=Mock(return_value=False),
                          stop_and_confirm=AsyncMock(return_value={"result": "ok"}))
    monkeypatch.setattr(free_explore, "go2", robot)
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: None)
    monkeypatch.setattr("cloud.go2.connection.go2", robot)
    monkeypatch.setattr("cloud.go2.navigation.navigator.navigator", nav)
    monkeypatch.setattr("cloud.agents.common.checkpoint.FileCheckpointSaver", lambda: saver)
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: SimpleNamespace(
        record_event=Mock(), current=lambda **_kw: None))
    monkeypatch.setattr(color_hunt, "activity_allows", lambda aid, *_args: aid == current[0])
    color = {"rgb": (255, 0, 0), "source": "esp32_desk_led", "as_of": time.time()}
    monkeypatch.setattr(color_hunt, "live_lamp_color", lambda _source: color)
    monkeypatch.setattr(color_hunt, "_wait_for_lamp_color", AsyncMock(return_value=(color, "")))
    messenger = SimpleNamespace(send_result=Mock(), send_message=Mock())
    event = {"event_id": "message_A", "message_id": "message_A", "source": "esp32_desk_led",
             "activity_id": "act_A", "deadline": time.time() + 60,
             "message": {"sender": "esp32_desk_led", "content": "找红色物品"}, "messenger": messenger}

    def switch(*, new_goal=True, same_owner=False):
        current[0] = "act_A" if same_owner else "act_B"
        drive._generation += 1
        drive.owner_session_id = current[0]
        drive._context = ExecutionContext(current[0], "visitor", False, origin_kind="agent_autonomous")
        drive._active_goal = ({"event_id": "message_B", "activity_id": current[0],
            "generation": drive._generation, "owner_session_id": current[0]} if new_goal else None)
        drive._last_tool_result = None

    return SimpleNamespace(drive=drive, saver=saver, events=events, robot=robot,
                           nav=nav, event=event, color=color, switch=switch)


async def old_receipt(hunt):
    checkpoint = await hunt.saver.aget_tuple({"configurable": {"thread_id": "go2:task:message_A"}})
    return checkpoint.checkpoint["channel_values"]["last_tool_result"]


@pytest.mark.parametrize("new_goal", [True, False])
async def test_waiting_for_color_cannot_finish_the_next_activity_goal(hunt, monkeypatch, new_goal):
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(*_args):
        entered.set()
        await release.wait()
        return None, "old_color_unavailable"

    monkeypatch.setattr(color_hunt, "_wait_for_lamp_color", wait)
    task = asyncio.create_task(color_hunt._run_color_hunt(hunt.drive, hunt.event, "peer_A"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    hunt.switch(new_goal=new_goal)
    expected = hunt.drive._active_goal
    release.set()
    await task
    assert hunt.drive._active_goal is expected
    assert hunt.drive._last_tool_result is None
    assert (await old_receipt(hunt))["reason"] == "activity_ended"
    assert not hunt.events


@pytest.mark.parametrize("new_goal,same_owner", [(True, False), (False, False), (True, True)])
async def test_late_graph_and_observation_keep_original_task_owner(hunt, monkeypatch, new_goal, same_owner):
    entered, release = asyncio.Event(), asyncio.Event()

    async def graph(_session, _goal, **kwargs):
        entered.set()
        await release.wait()
        kwargs["on_step"]({"tool": "go2_observe", "result": {
            "result": "ok", "message": "旧活动看到红盒子"}})
        assert kwargs["tool_guard"]("go2_observe", {})["error"] == "activity_ended"
        return {"result": "blocked", "error": "old_graph_blocked", "tool_results": []}

    monkeypatch.setattr(color_hunt, "run_device_task", graph)
    task = asyncio.create_task(color_hunt._run_color_hunt(hunt.drive, hunt.event, "peer_A"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    hunt.switch(new_goal=new_goal, same_owner=same_owner)
    expected = hunt.drive._active_goal
    release.set()
    await task
    assert hunt.drive._active_goal is expected
    assert hunt.drive._last_tool_result is None
    assert (await old_receipt(hunt))["reason"] == "stop_unconfirmed:activity_ended"
    assert not hunt.events
    hunt.robot.send_command.assert_not_called()
    hunt.nav.stop_and_confirm.assert_not_called()


async def test_current_color_hunt_preserves_real_confirmed_result(hunt, monkeypatch):
    async def graph(_session, _goal, **kwargs):
        assert kwargs["tool_guard"]("go2_observe", {}) is None
        assert kwargs["result_transform"]("go2_observe", {}, {
            "result": "ok", "message": "看到红色盒子"})["final"] is False
        kwargs["on_step"]({"tool": "go2_observe", "result": {
            "result": "ok", "message": "地面有一个红色盒子"}})
        assert kwargs["tool_guard"]("go2_navigate_to_visible_target", {
            "target": "红色盒子", "decision_reason": "刚看见的红盒子与题目颜色一致。"}) is None
        transform = kwargs["result_transform"]
        assert transform("go2_navigate_to_visible_target", {"target": "红色盒子"}, {
            "result": "ok", "target": "红色盒子", "progress": "approached"})["final"] is False
        assert transform("go2_navigate_to_visible_target", {"target": "红色盒子"}, {
            "result": "blocked", "reason": "path_blocked"})["final"] is False
        assert transform("go2_navigate_to_visible_target", {"target": "红色盒子"}, {
            "result": "unknown"})["final"] is False
        assert transform("go2_navigate_to_visible_target", {"target": "红色盒子"}, {
            "result": "ok", "target": "红色盒子",
            "confirmation": "odom_stopped_and_visual_recheck"})["final"] is True
        return {"result": "ok", "response": "这局找到了。", "emotion": "happy",
            "tool_results": [{"tool": "go2_navigate_to_visible_target",
            "result": {"result": "ok", "target": "红色盒子", "confirmation": "odom_stopped_and_visual_recheck"}}]}

    monkeypatch.setattr(color_hunt, "run_device_task", graph)
    await color_hunt._run_color_hunt(hunt.drive, hunt.event, "peer_A")
    assert hunt.drive._last_tool_result["reason"] == "candidate_reached"
    assert (await old_receipt(hunt))["result"] == "ok"
    assert hunt.drive._active_goal is None
    assert all(activity == "act_A" for activity, _args, _kwargs in hunt.events)
