"""Activity goals arbitrate before exploratory motion; no live models/devices."""

import asyncio
import importlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.go2.workflows.free_explore import build_motivation_graph


class MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread_id):
        yield


@pytest.mark.parametrize("content,action", [
    ("你两玩石头剪刀布", "social"),
    ("和同伴商量刚才的事情", "observe"),
    ("看看附近还有什么没去过的地方", "explore"),
])
async def test_activity_original_goal_reaches_existing_graph(content, action):
    seen = []

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            seen.append(json.loads(messages[-1].content))
            return {"action": action, "reason": "根据本次活动原话决定。"}

    graph = build_motivation_graph(profile_loader=lambda: {"revision": 1, "traits": {}},
                                   model=Model(), checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "activity_test"}}
    result = await graph.ainvoke({"connected": True,
        "activity_request": {"activity_id": "act_test", "content": content},
        "pending_event": {"kind": "activity_started", "activity_id": "act_test"}}, config)
    assert seen[0]["activity_request"]["content"] == content
    assert result["next_action"] == action
    assert result["should_explore"] is False  # Execution follows the chosen adapter, not a tick flag.
    tick = await graph.ainvoke({"pending_event": {}, "curiosity": 999,
                               "initial_exploration": True}, config)
    assert tick["should_explore"] is False
    assert len(seen) == 1


@pytest.mark.parametrize("response", [None, {"action": "spin", "reason": "invalid"},
                                     {"action": "explore", "reason": "", "extra": True}])
async def test_activity_model_unavailable_or_invalid_waits(response):
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return response

    graph = build_motivation_graph(profile_loader=lambda: {"revision": 1, "traits": {}},
                                   model=Model() if response is not None else None)
    result = await graph.ainvoke({"connected": True,
        "activity_request": {"activity_id": "act_test", "content": "一起玩"},
        "pending_event": {"kind": "activity_started", "activity_id": "act_test"}})
    assert result["next_action"] == "wait" and not result["should_explore"]


async def test_activity_mode_constructs_start_event_from_current_store(monkeypatch):
    service = importlib.import_module("cloud.go2.service")
    activity_module = importlib.import_module("cloud.space.activity")
    activity = {"activity_id": "act_current", "status": "starting",
                "source_event_id": "evt_user", "context": "你两玩石头剪刀布"}
    monkeypatch.setattr(activity_module, "get_activity_store",
                        lambda: SimpleNamespace(current=lambda: activity))
    monkeypatch.setattr(service, "go2", SimpleNamespace(is_connected=True))
    mode = ["manual"]
    monkeypatch.setattr(service, "get_autonomy_mode", lambda: mode[0])
    monkeypatch.setattr(service, "_activity_mode_owner", None)
    monkeypatch.setattr(service, "control_lease", SimpleNamespace(snapshot=lambda: None))
    configure = Mock(side_effect=lambda selected, **_kwargs: mode.__setitem__(0, selected))
    monkeypatch.setattr(service, "_configure_vision_consumers", configure)
    result = await service.set_activity_mode("free_explore", "act_current")
    assert result["ok"] is True
    assert configure.call_args.kwargs["activity_request"] == {
        "activity_id": "act_current", "source_event_id": "evt_user", "content": activity["context"]}
    assert configure.call_args.kwargs["context"].persist is False
    configure.reset_mock()
    activity["activity_id"] = "act_newer"
    result = await service.set_activity_mode("free_explore", "act_stale")
    assert result["ok"] is False
    configure.assert_not_called()


@pytest.fixture
def drive_harness(monkeypatch):
    module = importlib.import_module("cloud.go2.mode.free_explore")
    drive = module.Drive()
    monkeypatch.setattr(module, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(module, "runtime_for", lambda _robot: None)
    monkeypatch.setattr(drive, "_activity_id", lambda: "act_test")
    monkeypatch.setattr(drive, "_do_explore", AsyncMock())
    monkeypatch.setattr(drive, "_do_social", AsyncMock())
    event = {"event_id": "activity:act_test", "kind": "activity_started",
             "activity_id": "act_test", "deadline": time.time() + 60}
    return module, drive, event


async def test_start_queues_activity_goal_without_initial_patrol(drive_harness, monkeypatch):
    _module, drive, _event = drive_harness
    monkeypatch.setattr(drive, "_run_loop", AsyncMock())
    drive.start("act_test", activity_request={"activity_id": "act_test", "content": "一起玩"})
    await drive._task
    assert drive._initial_exploration is False
    assert drive._events[0]["kind"] == "activity_started"
    assert drive._events[0]["content"] == "一起玩"
    drive._do_explore.assert_not_awaited()


async def test_direct_free_explore_still_starts_its_existing_exploration(drive_harness, monkeypatch):
    _module, drive, _event = drive_harness
    monkeypatch.setattr(drive, "_run_loop", AsyncMock())
    drive.start("explicit_exploration")
    await drive._task
    assert drive._initial_exploration is True and not drive._events


async def test_goal_selected_exploration_uses_existing_adapter(drive_harness, monkeypatch):
    _module, drive, event = drive_harness
    monkeypatch.setattr(drive, "_decide_event", AsyncMock(return_value={"action": "explore"}))
    await drive._handle_activity_event(event, MemorySaver(), "test_activity")
    drive._do_explore.assert_awaited_once_with(defer_social=True)
    drive._do_social.assert_not_awaited()


async def test_each_real_exploration_result_returns_to_activity_graph_once(drive_harness, monkeypatch):
    _module, drive, event = drive_harness
    monkeypatch.setattr(drive, "_decide_event", AsyncMock(return_value={"action": "explore"}))

    async def explore(**_kwargs):
        drive._last_tool_result = {"task_id": "search_segment", "result": "ok",
                                   "reason": "coverage_exhausted"}

    monkeypatch.setattr(drive, "_do_explore", explore)
    await drive._handle_activity_event(event, MemorySaver(), "test_activity")
    assert drive._events[0]["kind"] == "activity_progress"
    assert json.loads(drive._events[0]["content"])["task_id"] == "search_segment"
    await drive._handle_activity_event(event, MemorySaver(), "test_activity")
    assert len(drive._events) == 1  # The same task cannot manufacture a new opportunity.


async def test_wait_consumes_considered_idle_opportunity(drive_harness, monkeypatch):
    module, drive, event = drive_harness
    activity_module = importlib.import_module("cloud.space.activity")
    game_base = importlib.import_module("cloud.peers.games.base")
    activity = {"activity_id": "act_test", "context": "先等同伴准备好。", "started_at": time.time()}
    peers = [{"unit_id": "esp32_desk_led"}]

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return {"action": "wait", "reason": "先等对方。"}

    monkeypatch.setattr(activity_module, "get_activity_store",
                        lambda: SimpleNamespace(current=lambda: activity))
    monkeypatch.setattr(module, "activity_peers", lambda *_args: (activity, peers))
    monkeypatch.setattr(module, "get_text_llm", lambda: Model())
    monkeypatch.setattr(module, "get_profile", lambda: {"revision": 1, "traits": {}})
    monkeypatch.setattr(module, "prompt_space_context", lambda: json.dumps({
        "peers": peers, "recent_discoveries": []}))
    monkeypatch.setattr(game_base, "game_invitation_ready", lambda *_args: True)
    result = await drive._decide_event(event, saver=MemorySaver())
    assert result["action"] == "wait"
    assert drive._social_state.opportunity(activity, peers, [], idle_ready=True) is None
    discovery = {"event_id": "evt_new", "event_type": "observation", "activity_id": "act_test",
                 "ts": time.time(), "text": "同伴刚完成准备。"}
    assert drive._social_state.opportunity(activity, peers, [discovery], idle_ready=True)


async def test_late_model_decision_cannot_restart_ended_activity(drive_harness, monkeypatch):
    _module, drive, event = drive_harness

    async def decide(*_args, **_kwargs):
        drive._generation += 1
        return {"action": "explore"}

    monkeypatch.setattr(drive, "_decide_event", decide)
    await drive._handle_activity_event(event, MemorySaver(), "test_activity")
    drive._do_explore.assert_not_awaited()


async def test_message_arriving_during_start_decision_precedes_motion(drive_harness, monkeypatch):
    _module, drive, event = drive_harness

    async def decide(*_args, **_kwargs):
        drive._events.append({"kind": "peer_message", "event_id": "peer_arrived"})
        return {"action": "explore"}

    monkeypatch.setattr(drive, "_decide_event", decide)
    await drive._handle_activity_event(event, MemorySaver(), "test_activity")
    drive._do_explore.assert_not_awaited()


async def test_activity_observation_is_stationary_and_reenters_same_graph(drive_harness, monkeypatch):
    module, drive, event = drive_harness
    execute = AsyncMock(return_value={"result": "ok", "message": "桌边的灯可见。"})
    monkeypatch.setattr(module, "execute_tool", execute)
    monkeypatch.setattr(drive, "_decide_event", AsyncMock(return_value={"action": "observe"}))
    monkeypatch.setattr(drive, "_activity_event", lambda *_args, **_kwargs:
                        {"event_id": "evt_observed", "ts": time.time()})
    await drive._handle_activity_event(event, MemorySaver(), "test_activity")
    assert execute.call_args.args[0] == "go2_observe"
    drive._do_explore.assert_not_awaited()
    assert drive._events[0]["kind"] == "activity_observed"
    assert drive._events[0]["content"] == "桌边的灯可见。"


async def test_queued_message_precedes_even_initial_exploration(drive_harness, monkeypatch):
    _module, drive, _event = drive_harness
    checkpoints = importlib.import_module("cloud.agents.common.checkpoint")
    monkeypatch.setattr(checkpoints, "FileCheckpointSaver", MemorySaver)
    drive._initial_exploration = True
    drive._events.append({"kind": "peer_message", "event_id": "inbound"})
    handler = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(drive, "_handle_event", handler)
    with pytest.raises(asyncio.CancelledError):
        await drive._run_loop()
    handler.assert_awaited_once()
    drive._do_explore.assert_not_awaited()


async def test_activity_expired_or_interrupted_cannot_start_motion(drive_harness, monkeypatch):
    _module, drive, event = drive_harness
    decision = AsyncMock(return_value={"action": "explore"})
    monkeypatch.setattr(drive, "_decide_event", decision)
    event["deadline"] = time.time() - 1
    await drive._handle_activity_event(event, MemorySaver(), "test")
    event["deadline"] = time.time() + 60
    drive.user_interrupt = True
    await drive._handle_activity_event(event, MemorySaver(), "test")
    decision.assert_not_awaited()
    drive._do_explore.assert_not_awaited()
