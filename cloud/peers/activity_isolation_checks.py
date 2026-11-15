"""Peer replies and new lamp effects retain their original activity owner."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cloud.peers.inbound import _finish, handle_api_peer_message
from cloud.peers.messaging import AgentMessenger
from cloud.mqtt_signing import sign_message

LAMP, DOG = "esp32_desk_led", "go2"


def activity(activity_id):
    return {"activity_id": activity_id, "status": "active", "devices": {
        unit: {"status": "joined"} for unit in (LAMP, DOG)}}


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "peer-activity-isolation-test-secret")
    current, published, received, recorded = [activity("act_A")], [], [], []

    def record_event(activity_id, event_type, **facts):
        if current[0] and current[0]["activity_id"] == activity_id:
            recorded.append({"activity_id": activity_id, "event_type": event_type, **facts})
            return recorded[-1]
        return None

    store = SimpleNamespace(current=lambda: current[0], record_event=record_event)
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    mqtt = SimpleNamespace(publish=lambda topic, raw, **_kwargs:
        published.append((topic, raw)) or SimpleNamespace(rc=0))
    messenger = AgentMessenger(mqtt, None, None, {LAMP, DOG}, received.append)
    messenger._allowed = lambda *_args: True

    def deliver():
        assert messenger.handle_message(*published[-1])
        messenger._workers.submit(lambda: None).result()

    def start():
        assert messenger.send_message(DOG, LAMP, "能帮我开一下灯吗？")["result"] == "accepted"
        original = json.loads(published[-1][1])
        deliver()
        return original

    yield SimpleNamespace(messenger=messenger, current=current, published=published,
                          recorded=recorded, received=received, start=start, deliver=deliver)
    messenger.close()


@pytest.mark.parametrize("next_activity", [None, "act_B", "ending"])
def test_ended_activity_cannot_publish_a_new_reply(bus, next_activity):
    original = bus.start()
    if next_activity == "ending":
        bus.current[0]["status"] = "ending"
    else:
        bus.current[0] = activity(next_activity) if next_activity else None
    count = len(bus.published)
    result = bus.messenger.send_message(LAMP, DOG, "A 的晚到回答",
                                        reply_to=original["message_id"])
    assert result == {"result": "blocked", "error": "activity_ended"}
    assert len(bus.published) == count


def test_live_reply_inherits_activity_conversation_and_deadline(bus):
    original = bus.start()
    result = bus.messenger.send_message(LAMP, DOG, "可以，先准备一下。",
        reply_to=original["message_id"], deadline=original["deadline"] + 500)
    assert result["result"] == "accepted"
    payload = json.loads(bus.published[-1][1])
    assert payload["activity_id"] == original["activity_id"] == "act_A"
    assert payload["conversation_id"] == original["conversation_id"]
    assert payload["reply_to"] == original["message_id"]
    assert payload["deadline"] == original["deadline"]


def test_unscoped_conversation_does_not_acquire_a_new_activity(bus):
    bus.current[0] = None
    original = bus.start()
    bus.current[0] = activity("act_B")
    assert bus.messenger.send_message(LAMP, DOG, "无活动对话仍沿原会话回复。",
        reply_to=original["message_id"])["result"] == "accepted"
    payload = json.loads(bus.published[-1][1])
    assert "activity_id" not in payload
    assert payload["conversation_id"] == original["conversation_id"]


def test_receiver_rejects_signed_reply_with_another_activity_owner(bus):
    original = bus.start()
    assert bus.messenger.send_message(LAMP, DOG, "同活动回复。",
        reply_to=original["message_id"])["result"] == "accepted"
    topic, raw = bus.published[-1]
    payload = json.loads(raw)
    payload["activity_id"] = "act_B"
    received = len(bus.received)
    assert bus.messenger.handle_message(topic, json.dumps(sign_message(topic, payload)))
    bus.messenger._workers.submit(lambda: None).result()
    assert len(bus.received) == received
    assert payload["message_id"] not in bus.messenger._inbound


@pytest.mark.parametrize("next_activity", [None, "act_B"])
def test_late_physical_receipt_keeps_original_task_without_new_activity_reply(bus, next_activity):
    original = bus.start()
    assert bus.messenger.send_progress(original["message_id"], "task_A")["result"] == "accepted"
    bus.deliver()
    bus.current[0] = activity(next_activity) if next_activity else None
    before = len(bus.recorded)
    count = len(bus.published)
    _finish(bus.messenger, original, {"result": "ok", "reply": "旧动作确已完成。", "task_id": "task_A"})
    new_payloads = [json.loads(raw) for _topic, raw in bus.published[count:]]
    assert [payload["kind"] for payload in new_payloads] == ["progress", "result"]
    receipt = new_payloads[-1]
    assert receipt["message_id"] == original["message_id"]
    assert receipt["conversation_id"] == original["conversation_id"]
    assert receipt["task_id"] == "task_A" and receipt["result"] == "ok"
    bus.deliver()
    assert bus.messenger.get_status(original["message_id"])["state"] == "ok"
    assert len(bus.recorded) == before


@pytest.fixture
def lamp_handler(monkeypatch, bus):
    dispatched = []
    agent = SimpleNamespace(_command_epoch=0, _autonomy_epoch=0,
                            get_autonomy_mode=lambda: "reactive")

    def mark_peer_command(_unit):
        agent._command_epoch += 1
        agent._autonomy_epoch += 1

    agent.mark_peer_command = mark_peer_command
    service = ModuleType("cloud.esp32.service")
    service.get_agent = lambda: agent
    intents = ModuleType("cloud.api.intents")

    def send(unit, action, params, session, *, task_id, **_kwargs):
        dispatched.append({"unit": unit, "action": action, "session": session, "task_id": task_id})
        return task_id

    intents._dispatch_mqtt_task = send
    monkeypatch.setitem(sys.modules, "cloud.esp32.service", service)
    monkeypatch.setitem(sys.modules, "cloud.api.intents", intents)
    return agent, dispatched


@pytest.mark.parametrize("next_activity", [None, "act_B"])
def test_old_lamp_turn_cannot_dispatch_after_activity_switch(bus, lamp_handler, next_activity):
    agent, dispatched = lamp_handler
    original = bus.start()

    def model_return(text, unit, context, dispatch, **_kwargs):
        bus.current[0] = activity(next_activity) if next_activity else None
        with pytest.raises(PermissionError, match="peer_context_superseded"):
            dispatch(unit, "SET_STATE", {"state": "ON"}, context, "lamp_chat", task_id="late_A")
        return {"result": "cancelled", "error": "peer_context_superseded"}

    agent.run_user_loop = model_return
    handle_api_peer_message(bus.messenger, original, None)
    assert dispatched == []
    assert not any(row.get("text") == "旧动作确已完成。" for row in bus.recorded)


@pytest.mark.parametrize("scoped", [True, False])
def test_current_lamp_turn_can_dispatch_serial_steps_and_reply(bus, lamp_handler, scoped):
    agent, dispatched = lamp_handler
    if not scoped:
        bus.current[0] = None
    original = bus.start()

    def model_return(text, unit, context, dispatch, **_kwargs):
        for index in range(2):
            dispatch(unit, "SET_STATE", {"state": "ON"}, context, "lamp_chat", task_id=f"task_{index}")
        return {"result": "ok", "reply": "已经调好了。", "task_id": "task_1"}

    agent.run_user_loop = model_return
    handle_api_peer_message(bus.messenger, original, None)
    assert [row["task_id"] for row in dispatched] == ["task_0", "task_1"]
    reply = json.loads(bus.published[-1][1])
    assert reply["kind"] == "message" and reply["reply_to"] == original["message_id"]
    assert reply.get("activity_id") == ("act_A" if scoped else None)


def test_lamp_turn_rechecks_control_epoch_even_when_mode_returns_to_reactive(bus, lamp_handler):
    agent, dispatched = lamp_handler
    original = bus.start()

    def model_return(text, unit, context, dispatch, **_kwargs):
        agent._autonomy_epoch += 2
        with pytest.raises(PermissionError, match="peer_context_superseded"):
            dispatch(unit, "SET_STATE", {"state": "ON"}, context, "lamp_chat", task_id="late_epoch")
        return {"result": "cancelled", "error": "peer_context_superseded"}

    agent.run_user_loop = model_return
    handle_api_peer_message(bus.messenger, original, None)
    assert dispatched == []


def test_expired_lamp_turn_blocks_other_effects_but_keeps_queries(bus, lamp_handler, monkeypatch):
    from cloud.agents.lamp.tools.execute import execute_lamp_tool

    agent, dispatched = lamp_handler
    original = bus.start()
    queried = []

    def auxiliary(**kwargs):
        queried.append(kwargs["tool"])
        return {"result": "ok", "space": {}}

    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_auxiliary", auxiliary)

    def model_return(text, unit, context, dispatch, **_kwargs):
        bus.current[0] = activity("act_B")
        for tool, params in (
            ("set_led_state", {"state": "ON"}),
            ("request_peer_skill", {"target_unit_id": DOG, "skill_id": "go2_sport", "params": {"cmd": "Hello"}}),
            ("send_message", {"target": DOG, "content": "旧活动不要另发消息。"}),
        ):
            result = execute_lamp_tool(tool, params, "effect_A", agent=agent,
                unit_id=unit, context=context, dispatch=dispatch, lighting_only=False, run_id="run_A")
            assert result == {"result": "cancelled", "error": "peer_context_superseded"}
        # Messaging also protects direct tool adapters, including a target
        # other than this turn's original conversational partner.
        assert bus.messenger.send_message(LAMP, "cloud_orchestrator", "旧活动的另一个出口。")["error"] == "peer_context_superseded"
        query = execute_lamp_tool("get_space_context", {}, "query_A", agent=agent,
            unit_id=unit, context=context, dispatch=dispatch, lighting_only=False, run_id="run_A")
        assert query["result"] == "ok"
        return {"result": "cancelled", "error": "peer_context_superseded"}

    agent.run_user_loop = model_return
    handle_api_peer_message(bus.messenger, original, None)
    assert queried == ["get_space_context"]
    assert dispatched == []


def test_control_takeover_during_publish_cannot_be_adopted_by_old_peer_turn(bus, lamp_handler, monkeypatch):
    agent, dispatched = lamp_handler
    original = bus.start()
    original_send = sys.modules["cloud.api.intents"]._dispatch_mqtt_task

    def publish_with_takeover(*args, **kwargs):
        outcome = original_send(*args, **kwargs)
        # Another controller changes the lamp while this thread publishes.
        agent._command_epoch += 1
        agent._autonomy_epoch += 1
        return outcome

    monkeypatch.setattr(sys.modules["cloud.api.intents"], "_dispatch_mqtt_task", publish_with_takeover)

    def model_return(text, unit, context, dispatch, **_kwargs):
        dispatch(unit, "SET_STATE", {"state": "ON"}, context, "lamp_chat", task_id="already_sent")
        with pytest.raises(PermissionError, match="peer_context_superseded"):
            dispatch(unit, "SET_STATE", {"state": "OFF"}, context, "lamp_chat", task_id="must_not_send")
        return {"result": "ok", "reply": "原动作已派发。", "task_id": "already_sent"}

    agent.run_user_loop = model_return
    handle_api_peer_message(bus.messenger, original, None)
    assert [row["task_id"] for row in dispatched] == ["already_sent"]
