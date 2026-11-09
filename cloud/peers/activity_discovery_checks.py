"""Activity entry observes first; peer proposals remain voluntary and unified."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.go2.workflows.free_explore import build_motivation_graph
from cloud.go2.mode import free_explore
from cloud.peers.social import SocialState
from cloud.space.activity import ActivityStore
from cloud.space.execution import ExecutionContext

PROFILE = {"revision": 1, "traits": {"curiosity": 88, "extraversion": 70}}


def test_join_and_idle_time_do_not_start_social_but_new_discoveries_do():
    social = SocialState()
    now = social._born_at + 1
    activity = {"activity_id": "act_new", "started_at": now}
    peers = [{"unit_id": "go2"}]
    assert social.opportunity(activity, peers, [], 100, now=now) is None
    assert social.opportunity(activity, peers, [], 100, now=now + 900) is None
    discovery = {"event_id": "evt_seen", "activity_id": "act_new",
                 "event_type": "observation", "ts": now + 1, "summary": "看到红色盒子"}
    opportunity = social.opportunity(activity, peers, [discovery], now=now + 2)
    assert opportunity["event_ids"] == ["evt_seen"]
    social.evaluated(opportunity, now=now + 2)
    assert social.opportunity(activity, peers, [discovery], now=now + 3) is None
    assert social.opportunity(activity, peers, [], now=now + 1000) is None


def test_first_evaluation_keeps_new_discovery_but_restart_ignores_old_one():
    social = SocialState()
    now = social._born_at + 1
    activity = {"activity_id": "act_new", "started_at": now}
    peer = [{"unit_id": "go2"}]
    discovery = {"event_id": "evt_seen", "activity_id": "act_new",
                 "event_type": "observation", "ts": now + 1}
    assert social.opportunity(activity, peer, [discovery], now=now + 2)
    restarted = SocialState()
    restarted._born_at = now + 3
    assert restarted.opportunity(activity, peer, [discovery], now=now + 4) is None
    social.reset(None)
    assert social.opportunity(activity, peer, [{**discovery, "activity_id": "act_old"}],
                              now=now + 2) is None


@pytest.mark.asyncio
async def test_entry_explores_immediately_but_still_respects_interrupt_and_connection():
    graph = build_motivation_graph(profile_loader=lambda: PROFILE)
    entry = {"connected": True, "initial_exploration": True, "curiosity": 0}
    assert (await graph.ainvoke(entry))["should_explore"] is True
    assert (await graph.ainvoke({**entry, "interrupted": True}))["should_explore"] is False
    assert (await graph.ainvoke({**entry, "connected": False}))["should_explore"] is False
    assert (await graph.ainvoke({**entry, "initial_exploration": False}))["should_explore"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [True, False])
async def test_entry_explores_first_and_later_retry_cooldown_does_not_block_proposals(
        monkeypatch, tmp_path, initial):
    from cloud.agents.common import checkpoint
    saver = checkpoint.FileCheckpointSaver(tmp_path)
    monkeypatch.setattr(checkpoint, "FileCheckpointSaver", lambda: saver)
    monkeypatch.setattr(free_explore, "get_profile", lambda: PROFILE)
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    drive = free_explore.Drive()
    drive._initial_exploration = initial
    drive._vision_retry_after = 0 if initial else time.time() + 60
    drive._events.append({"kind": "peer_message"})
    order = []

    async def explore():
        order.append("explore")

    async def handle(*_):
        order.append("proposal")
        raise asyncio.CancelledError

    drive._do_explore = explore
    drive._handle_event = handle
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(drive._run_loop(), timeout=3)
    assert order == (["explore", "proposal"] if initial else ["proposal"])


def test_lamp_entry_does_not_call_model_or_send_message(monkeypatch):
    from cloud.agents.lamp.workflows.reactive import run_reactive_event
    model = SimpleNamespace(bind_tools=Mock(return_value=SimpleNamespace(invoke=Mock())))
    agent = SimpleNamespace(_autonomy_epoch=1, _find_led_unit=lambda: "esp32_desk_led",
        _should_act=lambda: True, _device_online=lambda: True, _beliefs_since_summary=0,
        _belief_history=[],
        _dispatch_tool=Mock(side_effect=AssertionError("entry must not send messages")))
    result = run_reactive_event(agent, {"lighting_eligible": False}, model=model,
        publish=Mock(), checkpointer=InMemorySaver())
    assert result["status"] == "ok"
    model.bind_tools.return_value.invoke.assert_not_called()
    agent._dispatch_tool.assert_not_called()


@pytest.mark.parametrize("has_discovery", [False, True])
def test_lamp_can_message_only_at_a_discovery_opportunity(monkeypatch, has_discovery):
    from cloud.agents.lamp.workflows import reactive
    model = SimpleNamespace(bind_tools=Mock(return_value=SimpleNamespace(invoke=Mock(
        return_value=AIMessage(content="", tool_calls=[{"name": "send_message", "id": "call_1",
            "args": {"target": "go2", "content": "刚看到的盒子是什么颜色？"}}])))))
    monkeypatch.setattr(reactive, "_prompt", lambda *_: [])
    dispatch = Mock(return_value=True)
    agent = SimpleNamespace(_autonomy_epoch=1, _find_led_unit=lambda: "esp32_desk_led",
        _should_act=lambda: True, _device_online=lambda: True, _beliefs_since_summary=0,
        _belief_history=[], _dispatch_tool=dispatch,
        _last_reactive_tool_result={"result": "accepted"})
    sense = {"lighting_eligible": True}
    if has_discovery:
        sense["social_opportunity"] = {"kind": "discovery", "event_ids": ["evt_view"]}
    result = reactive.run_reactive_event(agent, sense, model=model,
        publish=Mock(), checkpointer=InMemorySaver())
    assert result["status"] == ("accepted" if has_discovery else "blocked")
    assert dispatch.call_count == int(has_discovery)


@pytest.fixture
def activity_drive(monkeypatch, tmp_path):
    store = ActivityStore(tmp_path / "activity.json")
    revision = store.note_user_intervention("user_start")
    activity = store.begin("我要工作了，你们自己玩", "user_start", revision)
    activity_id = activity["activity_id"]
    for unit in ("go2", "esp32_desk_led"):
        store.set_device(activity_id, unit, mode="free_explore" if unit == "go2" else "reactive",
                         status="joined", owner=activity_id)
    store.activate(activity_id)
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.peers.social.get_activity_store", lambda: store)
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    drive = free_explore.Drive()
    drive.owner_session_id = activity_id
    drive._context = ExecutionContext(activity_id, "visitor", False, origin_kind="agent_autonomous")
    drive._evaluate_social = AsyncMock()
    return store, drive


@pytest.mark.asyncio
async def test_exploration_observation_reaches_lamp_context_before_search_finishes(monkeypatch, activity_drive):
    from cloud.agents.go2.workflows import search
    store, drive = activity_drive
    lamp = SocialState()
    assert lamp.opportunity(store.current(), [{"unit_id": "go2"}], []) is None

    async def explore(**kwargs):
        await kwargs["on_observation"]({"frame_id": "view_1", "pose": {"x": 0, "y": 0},
            "media_ref": "/api/photo", "observation": {"scene_summary": "桌旁有红色盒子。"}})
        discoveries = store.recent_discoveries("esp32_desk_led")
        assert discoveries[0]["summary"] == "桌旁有红色盒子。"
        assert lamp.opportunity(store.current(), [{"unit_id": "go2"}], discoveries)
        # Repeated views at the same place do not create another social topic.
        await kwargs["on_observation"]({"pose": {"x": 0, "y": 0},
            "observation": {"scene_summary": "桌旁仍有红色盒子。"}})
        return {"result": "blocked", "reason": "coverage_exhausted"}

    monkeypatch.setattr(search, "run_search", explore)
    await drive._do_explore_owned()
    assert len(store.recent_discoveries("esp32_desk_led")) == 1
    drive._evaluate_social.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_localization_allows_only_camera_observation(monkeypatch, activity_drive):
    from cloud.agents.go2.workflows import search
    from cloud.go2.perception import visual_target
    store, drive = activity_drive
    drive._initial_exploration = True
    monkeypatch.setattr(search, "run_search", AsyncMock(return_value={
        "result": "blocked", "reason": "map_session_unavailable"}))
    capture = AsyncMock(return_value={"frame": {"frame_id": "view_1"}, "pose": None,
        "observation": {"scene_summary": "看见蓝色椅子。"}})
    monkeypatch.setattr(visual_target, "observe_target", capture)
    await drive._do_explore_owned()
    capture.assert_awaited_once_with("", require_pose=False)
    assert store.recent_discoveries("esp32_desk_led")[0]["summary"] == "看见蓝色椅子。"
    assert drive._last_tool_result["reason"] == "map_session_unavailable"
    assert store.events(drive.owner_session_id)[-1]["status"] == "blocked"


@pytest.mark.asyncio
async def test_late_observation_after_user_intervention_is_not_shared(monkeypatch, activity_drive):
    from cloud.agents.go2.workflows import search
    store, drive = activity_drive

    async def explore(**kwargs):
        drive.user_interrupt = True
        await kwargs["on_observation"]({"observation": {"scene_summary": "旧画面"}})
        return {"result": "cancelled"}

    monkeypatch.setattr(search, "run_search", explore)
    await drive._do_explore_owned()
    assert store.recent_discoveries("esp32_desk_led") == []
    drive._evaluate_social.assert_not_awaited()


@pytest.mark.asyncio
async def test_social_model_failure_does_not_stop_exploration_loop(monkeypatch, activity_drive):
    from cloud.agents.go2.workflows import search
    _, drive = activity_drive

    async def explore(**kwargs):
        await kwargs["on_observation"]({"observation": {"scene_summary": "看见白色桌子。"}})
        return {"result": "blocked", "reason": "coverage_exhausted"}

    monkeypatch.setattr(search, "run_search", explore)
    drive._evaluate_social.side_effect = RuntimeError("model unavailable")
    await drive._do_explore_owned()
    assert drive._last_tool_result["reason"] == "coverage_exhausted"
    assert drive._vision_retry_after > time.time()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["accept", "decline", "defer"])
async def test_ordinary_suggestion_keeps_unified_accept_decline_defer(choice):
    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            return {"choice": choice, "reason": "依据当前探索任务自行判断"}

    graph = build_motivation_graph(profile_loader=lambda: PROFILE, model=Model())
    result = await graph.ainvoke({"connected": True, "pending_event": {
        "kind": "peer_message", "content": "去溜达一圈吧", "interaction_kind": "message"}})
    assert result["event_decision"] == choice
    assert result["next_action"] == ("run_peer_task" if choice == "accept" else "none")


@pytest.mark.asyncio
async def test_search_adapter_shares_only_real_observation_stages(monkeypatch):
    from cloud.agents.go2.workflows.search import DeviceSearch
    monkeypatch.setattr("cloud.api.intents.publish_agent_event", Mock())
    adapter = object.__new__(DeviceSearch)
    adapter.context = ExecutionContext("activity", "visitor", False, origin_kind="agent_autonomous")
    adapter.task_id, adapter.progress_sequence = "search_test", 0
    adapter.on_observation = AsyncMock()
    observation = {"frame_id": "view_1", "observation": {"scene_summary": "白色桌子"}}
    await adapter.progress("search_observe", {"mode": "explore"}, observation=observation)
    await adapter.progress("search_selected", {"mode": "explore", "observation": observation})
    adapter.on_observation.assert_awaited_once_with(observation)
