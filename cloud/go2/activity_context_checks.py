"""Activity working context is isolated from old runs and their late receipts."""

import ast
import asyncio
import importlib
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver


class MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread_id):
        yield


@pytest.fixture
def isolated_drive(monkeypatch):
    module = importlib.import_module("cloud.go2.mode.free_explore")
    drive, saver, prompts = module.Drive(), MemorySaver(), []
    activity = {"activity_id": "act_old", "context": "一起玩", "started_at": time.time()}
    monkeypatch.setattr(module, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(module, "runtime_for", lambda _robot: None)
    monkeypatch.setattr(module, "get_profile", lambda: {"revision": 1, "traits": {}})
    monkeypatch.setattr(module, "prompt_space_context", lambda: "{}")
    monkeypatch.setattr(module, "activity_peers", lambda *_args: (activity, []))
    monkeypatch.setattr("cloud.space.activity.get_activity_store",
                        lambda: SimpleNamespace(current=lambda: activity))
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", lambda *_args: False)
    monkeypatch.setattr("cloud.agents.common.checkpoint.FileCheckpointSaver", lambda: saver)
    monkeypatch.setattr("cloud.go2.navigation.navigator.navigator", SimpleNamespace(stop=Mock()))
    monkeypatch.setattr(drive, "_run_loop", AsyncMock())

    class Model:
        def with_structured_output(self, schema, **_kwargs):
            self.schema = schema
            return self

        async def ainvoke(self, messages):
            text = messages[-1].content
            prompts.append(json.loads(text) if text.startswith('{"') else ast.literal_eval(text))
            if self.schema.__name__ == "ActivityDecision":
                return {"action": "wait", "reason": "等待本次活动的新事实"}
            return {"choice": "accept", "reason": "愿意参与本次活动"}

    monkeypatch.setattr(module, "get_text_llm", Model)
    return SimpleNamespace(module=module, drive=drive, saver=saver,
                           activity=activity, prompts=prompts)


async def start(harness, owner):
    harness.activity["activity_id"] = owner
    harness.drive.start(owner)
    await harness.drive._task


def event(harness, kind="peer_message"):
    return {"kind": kind, "event_id": "event_" + harness.activity["activity_id"],
            "activity_id": harness.activity["activity_id"], "content": "一起玩吧",
            "deadline": time.time() + 60}


@pytest.mark.parametrize("kind", ["activity_started", "peer_message"])
async def test_new_activity_does_not_inject_previous_result_or_goal(isolated_drive, kind):
    h = isolated_drive
    await start(h, "act_old")
    old_goal = {"kind": "game", "event_id": "old_game"}
    await h.drive._advance_task(old_goal, stage="finish", result={
        "result": "rejected", "reason": "game_target_not_visible"})
    h.drive._active_goal = {"event_id": "old_pending_goal"}
    h.drive._last_decision = {"reason": "old_decision"}
    h.drive._last_observation = "旧活动看不到灯"
    h.drive._social_retry_after = time.time() + 30

    await start(h, "act_new")
    assert h.drive._active_goal is None and h.drive.last_decision is None
    assert h.drive._last_tool_result is None and h.drive._last_observation is None
    assert h.drive._social_retry_after == 0
    await h.drive._decide_event(event(h, kind))
    assert h.prompts[-1]["last_result"] is None
    assert h.prompts[-1].get("goal") is None
    assert "game_target_not_visible" not in str(h.prompts[-1])
    # Clearing planning context must not erase original task evidence.
    saved = await h.saver.aget_tuple({"configurable": {"thread_id": "go2:task:old_game"}})
    assert saved.checkpoint["channel_values"]["last_tool_result"]["reason"] == "game_target_not_visible"


async def test_same_activity_keeps_its_own_real_result_for_next_decision(isolated_drive):
    h = isolated_drive
    await start(h, "act_old")
    receipt = {"result": "ok", "reason": "lamp_won_performance_confirmed"}
    await h.drive._advance_task({"event_id": "current_task"}, stage="finish", result=receipt)
    await h.drive._decide_event(event(h))
    assert h.prompts[-1]["last_result"] == receipt


async def test_stop_clears_working_context_before_another_start(isolated_drive):
    h = isolated_drive
    await start(h, "act_old")
    h.drive._active_goal = {"event_id": "old_goal"}
    h.drive._last_tool_result = {"reason": "old_failure"}
    h.drive._last_decision = {"reason": "old_decision"}
    h.drive._last_observation = "old_observation"
    h.drive.stop()
    assert h.drive.owner_session_id is None
    assert all(getattr(h.drive, name) is None for name in (
        "_active_goal", "_last_tool_result", "_last_decision", "_last_observation"))


@pytest.mark.parametrize("next_owner", ["act_new", "act_old"])
async def test_old_task_finishing_after_restart_cannot_replace_current_context(isolated_drive, next_owner):
    h = isolated_drive
    await start(h, "act_old")
    old_goal = {"event_id": "old_task", "activity_id": "act_old"}
    await h.drive._advance_task(old_goal, stage="start")
    await start(h, next_owner)
    new_goal = {"event_id": "new_task", "activity_id": next_owner}
    await h.drive._advance_task(new_goal, stage="start")
    current_goal, current_result = h.drive._active_goal, h.drive._last_tool_result

    receipt = {"result": "unknown", "reason": "old_stop_unconfirmed"}
    await h.drive._advance_task(old_goal, stage="finish", result=receipt)
    assert h.drive._active_goal is current_goal
    assert h.drive._last_tool_result is current_result
    assert not h.drive._events
    saved = await h.saver.aget_tuple({"configurable": {"thread_id": "go2:task:old_task"}})
    assert saved.checkpoint["channel_values"]["last_tool_result"] == receipt


async def test_task_checkpoint_returning_during_switch_cannot_overwrite_new_context(isolated_drive, monkeypatch):
    h = isolated_drive
    await start(h, "act_old")
    entered, release = asyncio.Event(), asyncio.Event()
    workflow = importlib.import_module("cloud.agents.go2.workflows.free_explore")
    build = workflow.build_motivation_graph

    def delayed_graph(**kwargs):
        graph = build(**kwargs)

        async def invoke(*args, **kw):
            result = await graph.ainvoke(*args, **kw)
            entered.set()
            await release.wait()
            return result

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(workflow, "build_motivation_graph", delayed_graph)
    old = asyncio.create_task(h.drive._advance_task({"event_id": "old_task"}, stage="finish",
                                                  result={"reason": "old_failure"}))
    await entered.wait()
    await start(h, "act_new")
    h.drive._active_goal = {"event_id": "new_goal"}
    release.set()
    await old
    assert h.drive._active_goal == {"event_id": "new_goal"}
    assert h.drive._last_tool_result is None


async def test_late_model_response_does_not_set_new_activity_decision(isolated_drive, monkeypatch):
    h = isolated_drive
    await start(h, "act_old")
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            entered.set()
            await release.wait()
            return {"choice": "accept", "reason": "旧活动的迟到决定"}

    monkeypatch.setattr(h.module, "get_text_llm", Model)
    old = asyncio.create_task(h.drive._decide_event(event(h)))
    await entered.wait()
    await start(h, "act_new")
    release.set()
    decision = await old
    assert decision["action"] == "none"
    assert h.drive.last_decision is None
    assert h.drive._active_goal is None and h.drive._last_tool_result is None


async def test_queued_old_activity_event_does_not_reach_model(isolated_drive):
    h = isolated_drive
    await start(h, "act_old")
    old_event = event(h)
    await start(h, "act_new")
    result = await h.drive._decide_event(old_event)
    assert result["action"] == "none"
    assert not h.prompts and h.drive.last_decision is None


async def test_late_motion_receipt_does_not_enter_new_activity_timeline(isolated_drive, monkeypatch):
    h = isolated_drive
    await start(h, "act_old")
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = {"result": "unknown", "reason": "old_stop_unconfirmed"}

    async def execute(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return receipt

    monkeypatch.setattr(h.module, "execute_tool", execute)
    record, publish = Mock(), AsyncMock()
    monkeypatch.setattr(h.drive, "_activity_event", record)
    monkeypatch.setattr(h.drive, "_publish_motion_reply", publish)
    old = asyncio.create_task(h.drive._execute_motion("go2_sport", {"cmd": "Hello"}))
    await entered.wait()
    await start(h, "act_new")
    record.reset_mock()
    release.set()
    assert await old == receipt  # The original caller can still reconcile its result.
    record.assert_not_called()
    publish.assert_not_called()
    assert h.drive._last_execution_result == "ok"


async def test_delayed_motion_reply_cannot_start_in_new_activity(isolated_drive, monkeypatch):
    h = isolated_drive
    await start(h, "act_old")
    old_scope = (h.drive._generation, h.drive.owner_session_id)
    await start(h, "act_new")
    publish = Mock()
    monkeypatch.setattr("cloud.api.intents.publish_agent_event", publish)
    await h.drive._publish_motion_reply("go2_sport", {}, {"result": "ok"}, run_scope=old_scope)
    publish.assert_not_called()


async def test_late_social_decision_does_not_consume_new_activity_opportunities(isolated_drive, monkeypatch):
    h = isolated_drive
    await start(h, "act_old")
    peers = [{"unit_id": "esp32_desk_led", "online": True}]
    monkeypatch.setattr(h.module, "get_profile", lambda: {"prompt": "Go2", "traits": {"extraversion": 60}})
    monkeypatch.setattr(h.module, "activity_peers", lambda *_args: (h.activity, peers))
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_args, **_kwargs: {
        "peers": peers, "recent_discoveries": [], "activity": dict(h.activity)})
    monkeypatch.setattr("cloud.api.main.get_agent_messenger", lambda: None)
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", lambda *_args: True)
    monkeypatch.setattr("cloud.peers.games.playable_games", lambda *_args: [])
    monkeypatch.setattr("cloud.go2.perception.identity.identity_skill.context", lambda: {})
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            entered.set()
            await release.wait()
            return {"decision": "skip", "reason": "旧活动暂不发信"}

    monkeypatch.setattr(h.module, "get_text_llm", Model)
    old = asyncio.create_task(h.drive._evaluate_social())
    await entered.wait()
    await start(h, "act_new")
    opportunity = h.drive._social_state.opportunity(h.activity, peers, [], idle_ready=True)
    assert opportunity is not None
    release.set()
    await old
    assert h.drive._social_state.last_evaluated == 0
    assert h.drive._social_state.opportunity(h.activity, peers, [], idle_ready=True) == opportunity


@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_old_game_cleanup_cannot_stop_new_activity_motion(isolated_drive, monkeypatch, tmp_path, failure):
    from cloud.agents.common.effects import FileEffectLedger

    h = isolated_drive
    await start(h, "act_old")
    h.module.go2.send_command = AsyncMock(return_value={"result": "ok"})
    # This check isolates motion ownership; lifecycle journaling still uses
    # the current store/messenger interface during cancellation cleanup.
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: SimpleNamespace(
        current=lambda: h.activity, get=lambda _aid: {}, events=lambda _aid: [], record_event=Mock()))
    monkeypatch.setattr("cloud.go2.control.control_lease.snapshot", lambda: None)
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", lambda *_args: True)
    monkeypatch.setattr("cloud.peers.games.game_is_playable", lambda *_args: True)
    monkeypatch.setattr("cloud.peers.inbound.message_activity_allows", lambda _message: True)
    monkeypatch.setattr("cloud.agents.common.effects.FileEffectLedger", lambda: FileEffectLedger(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*_args):
        entered.set()
        await release.wait()
        raise failure()

    monkeypatch.setattr("cloud.peers.games.get_game", lambda _game_id:
                        SimpleNamespace(game_id="example", execute=execute))
    messenger = SimpleNamespace(send_progress=Mock(), send_result=Mock(),
                                get_received_result=lambda _id: None, close_game=Mock())
    old_event = {**event(h), "interaction_kind": "game_invite", "game_id": "example",
        "message_id": "old_invite", "game_preaccepted": True,
        "original_invitation_id": "old_original", "decision": {"choice": "accept"},
        "message": {"sender": "esp32_desk_led", "target": "go2"}, "messenger": messenger}
    old = asyncio.create_task(h.drive._handle_event_owned(old_event, None, None, None))
    await entered.wait()
    await start(h, "act_new")
    h.drive._active_goal = {"event_id": "new_goal"}
    release.set()
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await old
    else:
        await old
    h.module.go2.send_command.assert_not_awaited()
    from cloud.go2.navigation.navigator import navigator
    navigator.stop.assert_called_once_with(owner_session_id="act_old")
    assert h.drive._active_goal == {"event_id": "new_goal"}
