"""Offline checks for correlated peer turns, queue priority and real receipts."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.agents.common.runner import invoke_blocking
from cloud.peers.inbound import _finish, handle_api_peer_message, message_activity_allows
from cloud.peers.messaging import AgentMessenger
from cloud.peers.turns import current_peer_turn, peer_turn

LAMP, DOG = "esp32_desk_led", "go2"


@pytest.fixture
def dialogue_bus(monkeypatch):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "peer-dialogue-offline-test-secret")
    activity = {"activity_id": "act_dialogue", "status": "active", "devices": {
        unit: {"status": "joined"} for unit in (LAMP, DOG)}}
    store = SimpleNamespace(current=lambda: activity, record_event=Mock(return_value={"recorded": True}))
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    monkeypatch.setattr("cloud.peers.social.get_activity_store", lambda: store)
    now, sent, received = [time.time()], [], []
    mqtt = SimpleNamespace(publish=lambda topic, raw, **_kwargs:
                           sent.append((topic, raw)) or SimpleNamespace(rc=0))
    bus = AgentMessenger(mqtt, None, None, {LAMP, DOG}, received.append, now=lambda: now[0])
    bus._allowed = lambda *_args: True

    def deliver_last():
        assert bus.handle_message(*sent[-1])
        bus._workers.submit(lambda: None).result()

    def start():
        result = bus.send_message(LAMP, DOG, "要继续讨论吗？")
        assert result["result"] == "accepted"
        original = json.loads(sent[-1][1])
        deliver_last()
        return original

    yield SimpleNamespace(bus=bus, sent=sent, received=received, now=now,
                          activity=activity, start=start, deliver_last=deliver_last, store=store)
    bus.close()


def test_pending_pair_blocks_new_topics_but_allows_same_thread_and_results(dialogue_bus):
    h, bus = dialogue_bus, dialogue_bus.bus
    original = h.start()
    assert bus.has_pending_conversation(DOG, LAMP)
    assert bus.send_message(DOG, LAMP, "另外开一局？")["error"] == "peer_conversation_pending"
    assert bus.send_progress(original["message_id"], "peer_task")["result"] == "accepted"
    h.deliver_last()
    assert bus.send_result(original["message_id"], "ok", task_id="peer_task")["result"] == "accepted"
    h.deliver_last()
    reply = bus.send_message(DOG, LAMP, "这里还有一个问题。", reply_to=original["message_id"])
    assert reply["result"] == "accepted"
    payload = json.loads(h.sent[-1][1])
    assert payload["conversation_id"] == original["conversation_id"]
    h.deliver_last()
    assert bus.has_pending_conversation(LAMP, DOG)
    bus.send_result(reply["message_id"], "ok")
    h.deliver_last()
    assert not bus.has_pending_conversation(DOG)


async def test_peer_turn_crosses_task_and_blocking_adapter_then_resets(dialogue_bus):
    h, bus = dialogue_bus, dialogue_bus.bus
    original = h.start()

    async def respond():
        await asyncio.sleep(0)
        outcome = await invoke_blocking(bus.send_message, DOG, LAMP, "沿用原对话继续。")
        duplicate = await invoke_blocking(bus.send_message, DOG, LAMP, "重复的最终总结。")
        return outcome, duplicate

    with peer_turn(bus, original) as turn:
        task = asyncio.create_task(respond())
    assert current_peer_turn(bus, DOG, LAMP) is None
    outcome, duplicate = await task
    assert outcome["result"] == "accepted" and turn.sent
    assert duplicate["error"] == "peer_turn_already_replied"
    payload = json.loads(h.sent[-1][1])
    assert payload["reply_to"] == original["message_id"]
    assert payload["conversation_id"] == original["conversation_id"]
    assert payload["deadline"] <= original["deadline"]


@pytest.mark.parametrize("deadline", [0, 1])
def test_correlated_reply_never_extends_explicit_expired_deadline(dialogue_bus, deadline):
    h = dialogue_bus
    original = h.start()
    published = len(h.sent)
    with peer_turn(h.bus, original) as turn:
        outcome = h.bus.send_message(DOG, LAMP, "过期回复不应重新获得期限。", deadline=deadline)
    assert outcome == {"result": "blocked", "error": "message_expired"}
    assert not turn.sent and len(h.sent) == published


@pytest.mark.parametrize("reply_to,already_sent", [(None, True), ("prior_message", False)])
def test_internal_final_does_not_echo_after_explicit_send_or_reply(dialogue_bus, reply_to, already_sent):
    h = dialogue_bus
    original = h.start()
    if reply_to:
        original["reply_to"] = reply_to
    h.sent.clear()
    _finish(h.bus, original, {"result": "ok", "reply": "仅用于结束当前处理。"},
            already_sent=already_sent)
    payloads = [json.loads(raw) for _topic, raw in h.sent]
    assert len(payloads) == 1 and payloads[0]["kind"] == "result"


def test_lamp_reply_runs_existing_graph_without_ack_model_or_automatic_echo(dialogue_bus, monkeypatch):
    h = dialogue_bus
    original = h.start()
    sent = h.bus.send_message(DOG, LAMP, "还有一个问题。", reply_to=original["message_id"])
    reply = json.loads(h.sent[-1][1])
    h.deliver_last()
    agent = SimpleNamespace(run_user_loop=Mock(return_value={"result": "ok", "reply": "已经说完了。"}))
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: agent)
    h.sent.clear()
    handle_api_peer_message(h.bus, reply, None)
    agent.run_user_loop.assert_called_once()
    assert agent.run_user_loop.call_args.args[2].session_id == "peer_" + original["conversation_id"]
    payloads = [json.loads(raw) for _topic, raw in h.sent]
    assert payloads and all(row["kind"] == "result" for row in payloads)
    assert sent["message_id"] == payloads[-1]["message_id"]


def test_pending_rows_release_on_deadline_or_activity_change(dialogue_bus):
    h = dialogue_bus
    original = h.start()
    h.now[0] = original["deadline"] + .1
    assert not h.bus.has_pending_conversation(DOG)
    h.now[0] = original["deadline"] - 1
    h.activity["activity_id"] = "act_next"
    assert not h.bus.has_pending_conversation(DOG)


def test_exception_after_physical_progress_reports_unknown_with_original_task(dialogue_bus, monkeypatch):
    h, bus = dialogue_bus, dialogue_bus.bus
    bus.send_message(DOG, LAMP, "请结合当前情况调整灯光。")
    message = json.loads(h.sent[-1][1])
    h.deliver_last()

    def run(_text, _unit, context, dispatch, **_kwargs):
        dispatch(LAMP, "SET_STATE", {"state": "ON"}, context, "peer_autonomous", task_id="physical_reserved")
        raise RuntimeError("reply handling failed after dispatch")

    agent = SimpleNamespace(run_user_loop=run, get_autonomy_mode=lambda: "reactive", mark_peer_command=Mock())
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: agent)
    monkeypatch.setattr("cloud.api.intents._dispatch_mqtt_task", lambda *_args, task_id=None, **_kwargs: task_id)
    handle_api_peer_message(bus, message, None)
    result = json.loads(h.sent[-1][1])
    assert (result["kind"], result["result"], result["task_id"]) == ("result", "unknown", "physical_reserved")
    h.deliver_last()
    assert bus.get_status(message["message_id"])["state"] == "unknown"


async def test_exploration_finish_prioritizes_queued_inbox(monkeypatch):
    from cloud.go2.mode import free_explore
    drive = free_explore.Drive()
    drive._events.append({"message_id": "queued_peer_message"})
    drive._do_explore_owned = AsyncMock(return_value={"event_id": "new_observation"})
    drive._evaluate_social = AsyncMock()
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: None)
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    await drive._do_explore()
    drive._evaluate_social.assert_not_awaited()


def test_go2_reply_is_queued_with_its_original_thread():
    from cloud.go2.mode.free_explore import Drive
    drive = Drive()
    drive.submit_event = Mock(return_value=True)
    message = {"message_id": "reply", "target": DOG, "sender": LAMP,
               "reply_to": "original", "reply_kind": "reply", "conversation_id": "conversation",
               "deadline": time.time() + 30, "content": "还需要我做什么？"}
    assert drive.submit_peer_message(message, object())
    queued = drive.submit_event.call_args.args[0]
    assert queued["reply_to"] == "original" and queued["message"] == message


async def test_steward_activity_message_reaches_go2_graph_without_joining_as_device(dialogue_bus, monkeypatch):
    from cloud.go2.mode import free_explore
    from cloud.space.execution import ExecutionContext
    h = dialogue_bus
    drive = free_explore.Drive()
    drive._context = ExecutionContext("act_dialogue", "visitor", False,
                                      origin_kind="agent_autonomous", expires_at=int(time.time()+60))
    drive._advance_task = AsyncMock()
    message = {"message_id": "steward_message", "sender": "cloud_orchestrator", "target": DOG,
               "conversation_id": "steward_message", "activity_id": "act_dialogue",
               "content": "请结合当前活动，自主考虑与灯玩一局。", "deadline": time.time()+30,
               "interaction_kind": "message"}
    assert message_activity_allows(message)
    assert not message_activity_allows({**message, "interaction_kind": "game_invite"})
    graph = AsyncMock(return_value={"result": "ok", "response": "我先和灯确认意愿。"})
    monkeypatch.setattr("cloud.agents.go2.graph.run_go2_loop", graph)
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: None)
    messenger = Mock()
    event = {"kind": "peer_message", "message_id": message["message_id"],
             "event_id": message["message_id"], "activity_id": "act_dialogue",
             "deadline": message["deadline"], "interaction_kind": "message",
             "message": message, "messenger": messenger,
             "decision": {"choice": "accept", "reason": "考虑当前用户活动"}}
    await drive._handle_event_owned(event, None, None, "test_thread")
    graph.assert_awaited_once()
    messenger.send_result.assert_called_once_with("steward_message", "ok",
        task_id="peer_steward_message", reason=None)


async def test_deferred_invitation_uses_existing_social_graph_after_turn_releases(dialogue_bus, monkeypatch, tmp_path):
    from cloud.go2.mode import free_explore
    from cloud.space.execution import ExecutionContext
    h, bus = dialogue_bus, dialogue_bus.bus
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    original = h.start()
    drive = free_explore.Drive()
    drive.owner_session_id = "act_dialogue"
    drive._context = ExecutionContext("act_dialogue", "visitor", False,
        origin_kind="agent_autonomous", expires_at=int(time.time()+60))
    drive._advance_task = AsyncMock()
    monkeypatch.setattr(free_explore, "go2", SimpleNamespace(is_connected=True))
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: None)
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready",
                        lambda *_: drive._active_goal is None)
    monkeypatch.setattr("cloud.peers.games.registry.game_invitation_ready",
                        lambda *_: drive._active_goal is None)
    bus._resolve_decision = lambda *_: {"reason": "用户要求这局游戏", "evidence": []}

    async def role_graph(*_args, **_kwargs):
        assert drive._active_goal is not None
        attempt = bus.send_message(DOG, LAMP, "要玩一局吗？", interaction_kind="game_invite", game_id="rps_siege")
        assert attempt == {"result": "blocked", "error": "peer_conversation_pending"}
        return {"result": "ok", "response": "先结束这一轮，再结合当前情况邀请。"}

    monkeypatch.setattr("cloud.agents.go2.graph.run_go2_loop", role_graph)
    before = len(h.sent)
    await drive._handle_event_owned({"kind": "peer_message", "message_id": original["message_id"],
        "event_id": original["message_id"], "activity_id": "act_dialogue", "deadline": original["deadline"],
        "interaction_kind": "message", "message": original, "messenger": bus,
        "decision": {"choice": "accept"}}, None, None, "test_peer_turn")
    assert drive._active_goal is None and drive._social_state.has_completed_turn("act_dialogue")
    assert not any(json.loads(raw).get("game_id") for _topic, raw in h.sent[before:])
    # Complete delivery of the single final reply; its consumer chooses no
    # continuation. No acknowledgement prose or new conversation is generated.
    final_reply = None
    for topic, raw in list(h.sent[before:]):
        bus.handle_message(topic, raw)
        if json.loads(raw).get("kind") == "message":
            final_reply = json.loads(raw)
    _finish(bus, final_reply, {"result": "ok", "reply": "仅内部结束。"})
    h.deliver_last()
    assert not bus.has_pending_conversation(DOG)
    activity = h.activity
    space = {"activity": activity, "peers": [{"unit_id": LAMP, "online": True, "skills": []}],
             "recent_discoveries": [], "interaction_history": {}, "decision_evidence": []}
    monkeypatch.setattr("cloud.api.main.get_agent_messenger", lambda: bus)
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_args, **_kwargs: space)
    monkeypatch.setattr(free_explore, "get_profile", lambda: {"prompt": "我是机器狗", "traits": {"extraversion": 75}})
    monkeypatch.setattr(free_explore, "prompt_space_context", lambda: "用户要求双方玩注册游戏。")
    monkeypatch.setattr("cloud.go2.perception.identity.identity_skill.context", lambda: {})
    monkeypatch.setattr("cloud.agents.go2.workflows.social.agent_names", lambda: {})

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            assert "peer_turn_completed" in messages[1].content
            return {"decision": "send_message", "target": LAMP, "content": "现在愿意玩一局吗？",
                    "interaction_kind": "game_invite", "game_id": "rps_siege",
                    "reason": "用户要求这局游戏", "evidence_refs": ["activity_request"]}

    monkeypatch.setattr(free_explore, "get_text_llm", Model)
    await drive._evaluate_social()
    payload = json.loads(h.sent[-1][1])
    assert payload["interaction_kind"] == "game_invite" and payload["game_id"] == "rps_siege"
    assert not drive._social_state.has_completed_turn("act_dialogue")
