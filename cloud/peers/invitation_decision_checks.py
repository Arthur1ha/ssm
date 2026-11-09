"""Context-grounded peer proposals retain evidence without authorizing motion."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.peers.messaging import AgentMessenger
from cloud.space.decisions import decision_evidence
from cloud.space.execution import ExecutionContext


@pytest.fixture
def space():
    now = time.time()
    return {
        "activity": {"activity_id": "act_test", "context": "你们自己玩一会儿", "started_at": now},
        "peers": [{"unit_id": "go2", "name": "机器狗", "online": True,
                   "state_summary": {"status": "current", "as_of": now, "life_state": "idle"}}],
        "environment": [], "constraints": [],
        "recent_discoveries": [{"event_id": "evt_seen", "activity_id": "act_test",
            "unit_id": "go2", "event_type": "observation", "ts": now,
            "summary": "地上有一个白色猫头鹰玩偶"}],
    }


@pytest.fixture
def bus(monkeypatch, space):
    monkeypatch.setenv("SSM_MQTT_SIGNING_SECRET", "grounded-invitation-test-secret")
    record = {**space["activity"], "status": "active", "devices": {
        "esp32_desk_led": {"status": "joined"}, "go2": {"status": "joined"}}}
    store = SimpleNamespace(current=lambda: record, record_event=Mock(return_value={"recorded": True}))
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: store)
    sent, delivered = [], []
    mqtt = SimpleNamespace(publish=lambda topic, body, **_: sent.append((topic, body)) or
                           SimpleNamespace(rc=0))
    messenger = AgentMessenger(mqtt, None, None, {"esp32_desk_led", "go2"}, delivered.append)
    messenger._allowed = lambda *_: True
    messenger._decision_context = lambda _: space
    yield messenger, sent, delivered, store
    messenger.close()


def invite(messenger, **kwargs):
    return messenger.send_message("esp32_desk_led", "go2",
        "来玩找颜色？我亮一种颜色，你去找同色物品，找到了就算你赢。",
        interaction_kind="game_invite", game_id="color_hunt", **kwargs)


def test_invite_requires_real_current_evidence_before_publish(bus, space):
    messenger, sent, _, _ = bus
    peer_ref = next(item["ref"] for item in decision_evidence(space)
                    if item["kind"] == "peer_state" and item["source"] == "go2")
    assert invite(messenger)["result"] == "rejected"
    assert invite(messenger, decision_reason="狗空闲，想邀请它玩", evidence_refs=["invented"])["result"] == "rejected"
    space["peers"][0]["state_summary"]["as_of"] -= 61
    assert invite(messenger, decision_reason="狗空闲，想邀请它玩", evidence_refs=[peer_ref])["result"] == "rejected"
    assert sent == []


def test_invite_basis_survives_signed_delivery_and_visible_activity(bus, space):
    messenger, sent, delivered, store = bus
    peer_ref = next(item["ref"] for item in decision_evidence(space)
                    if item["kind"] == "peer_state" and item["source"] == "go2")
    result = invite(messenger, decision_reason="狗分享了玩偶观察，现在显示空闲，问问它要不要玩",
                    evidence_refs=[peer_ref, "evt_seen"])
    assert result["result"] == "accepted"
    topic, body = sent[-1]
    basis = json.loads(body)["decision_basis"]
    assert [item["ref"] for item in basis["evidence"]] == [peer_ref, "evt_seen"]
    assert basis["evidence"][1]["source"] == "go2"
    assert store.record_event.call_args.kwargs["decision_basis"] == basis
    # A heartbeat with the same semantic state must not rewrite the evidence
    # timestamp to pretend this later snapshot informed the earlier decision.
    space["peers"][0]["state_summary"]["as_of"] = time.time()
    messenger.handle_message(topic, body)
    messenger._workers.submit(lambda: None).result()
    assert delivered[0]["decision_basis"] == basis
    assert delivered[0]["decision_basis"]["evidence"][0]["as_of"] != space["peers"][0]["state_summary"]["as_of"]


def test_invite_rejects_state_ref_when_peer_started_another_task(bus, space):
    messenger, sent, _, _ = bus
    peer_ref = next(item["ref"] for item in decision_evidence(space)
                    if item["kind"] == "peer_state" and item["source"] == "go2")
    space["peers"][0]["state_summary"]["life_state"] = "play"
    assert invite(messenger, decision_reason="狗空闲，问问它要不要玩", evidence_refs=[peer_ref])["result"] == "rejected"
    assert sent == []


def test_receiver_rejects_proposal_if_peer_state_changed_after_send(bus, space):
    messenger, sent, delivered, _ = bus
    peer_ref = next(item["ref"] for item in decision_evidence(space)
                    if item["kind"] == "peer_state" and item["source"] == "go2")
    assert invite(messenger, decision_reason="狗空闲，问问它要不要玩", evidence_refs=[peer_ref])["result"] == "accepted"
    space["peers"][0]["state_summary"]["life_state"] = "play"
    messenger.handle_message(*sent[-1])
    messenger._workers.submit(lambda: None).result()
    assert delivered == []


def test_receiver_rechecks_evidence_after_it_expires(bus, space):
    messenger, sent, delivered, _ = bus
    result = invite(messenger, decision_reason="狗分享了新观察，想邀请它玩", evidence_refs=["evt_seen"])
    assert result["result"] == "accepted"
    space["recent_discoveries"][0]["ts"] -= 601
    messenger.handle_message(*sent[-1])
    messenger._workers.submit(lambda: None).result()
    assert delivered == []


def test_ordinary_chat_needs_no_game_evidence(bus):
    messenger, _, _, _ = bus
    assert messenger.send_message("esp32_desk_led", "go2", "要不要聊聊？")["result"] == "accepted"


def test_no_local_sensor_does_not_hide_shared_social_context(monkeypatch, space):
    from cloud.agents.lamp.workflows.runtime import ReactiveMind
    from cloud.agents.lamp.workflows.reactive import prepare_sensor_event
    from cloud.peers.social import SocialState

    state = SimpleNamespace(sensor_snapshot=lambda: {}, actuator_snapshot=lambda: {})
    mind = ReactiveMind()
    mind._state, mind._last_sound_ts = state, 0
    sense = mind._sense()
    assert sense is not None and sense["light_available"] is False
    social = SocialState()
    agent = SimpleNamespace(_state=state, _sense=lambda: sense, _autonomy_epoch=1,
        _should_act=lambda: True, _device_online=lambda: True,
        _find_led_unit=lambda: "esp32_desk_led", _check_proactive=lambda _: [],
        _social_state=social, _last_reason_sig=None, _last_reason_ts=0)
    activity = {**space["activity"], "started_at": time.time(), "status": "active", "devices": {
        "esp32_desk_led": {"status": "joined"}, "go2": {"status": "joined"}}}
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: space)
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: SimpleNamespace(current=lambda: activity))
    monkeypatch.setattr("cloud.peers.social.get_activity_store", lambda: SimpleNamespace(current=lambda: activity))
    monkeypatch.setattr("cloud.peers.gateway.list_visible_peers", lambda _: space["peers"])
    monkeypatch.setattr("cloud.agents.lamp.personality.profile.get_profile", lambda: {
        "traits": {"social_initiative": 50, "adjustment_sensitivity": 50}, "revision": 1})
    event = prepare_sensor_event(agent, {})
    assert event is not None
    assert event[0]["social_opportunity"]["kind"] == "joined"
    assert event[0]["lighting_eligible"] is False
    agent._should_act = lambda: False
    assert prepare_sensor_event(agent, {}) is None


@pytest.mark.asyncio
async def test_starting_activity_does_not_schedule_a_game(monkeypatch):
    from cloud.api import activity
    record = {"activity_id": "act_test", "status": "active"}
    store = SimpleNamespace(matches_user_request=lambda *_: True,
        begin=lambda *_: record, activate=lambda *_: record)
    monkeypatch.setattr(activity, "get_activity_store", lambda: store)
    monkeypatch.setattr(activity, "_end_locked", AsyncMock())
    monkeypatch.setattr(activity, "_activity_cards", lambda: [])
    create = Mock(side_effect=AssertionError("activity start must not schedule a game"))
    monkeypatch.setattr(activity.asyncio, "create_task", create)
    assert await activity.start_activity("自己活动", "evt_test", 1) == record
    create.assert_not_called()


def test_game_color_evidence_is_checked_before_even_power_on(monkeypatch, space, bus):
    from cloud.agents.lamp.tools.execute import execute_lamp_tool
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: space)
    physical = Mock(return_value={"result": "ok"})
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    context = ExecutionContext("peer_game_color_test", "agent", False, "main", "agent_autonomous", int(time.time()+30))
    result = execute_lamp_tool("set_led_state", {"state": "ON"}, "step", agent=SimpleNamespace(_state=None),
        unit_id="esp32_desk_led", context=context, dispatch=Mock(), lighting_only=True, run_id="run")
    assert result["result"] == "rejected"
    physical.assert_not_called()
    bus[3].record_event.assert_not_called()


def test_game_color_metadata_is_not_dispatched_to_firmware(monkeypatch, space, bus):
    from cloud.agents.lamp.tools.execute import execute_lamp_tool
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: space)
    monkeypatch.setattr("cloud.cards.registry.get_registry", lambda: SimpleNamespace(get_card=lambda _: {"unit_id": "esp32_desk_led"}))
    physical = Mock(return_value={"result": "ok", "task_id": "physical_task"})
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    context = ExecutionContext("peer_game_color_test", "agent", False, "main", "agent_autonomous", int(time.time()+30))
    result = execute_lamp_tool("set_led_color", {"r": 255, "g": 255, "b": 255,
        "decision_reason": "狗刚观察到白色玩偶，选择有候选物的白色", "evidence_refs": ["evt_seen"]},
        "step", agent=SimpleNamespace(_state=None, _should_act=lambda: True, _device_online=lambda: True), unit_id="esp32_desk_led", context=context,
        dispatch=Mock(), lighting_only=True, run_id="run")
    assert result["result"] == "ok"
    assert physical.call_args.kwargs["params"] == {"r": 255, "g": 255, "b": 255}
    assert result["decision_basis"]["evidence"][0]["ref"] == "evt_seen"


@pytest.mark.parametrize("tool,arguments", [("set_led_state", {"state": "ON"}),
    ("set_led_color", {"r": 255, "g": 255, "b": 255})])
def test_game_decision_is_visible_before_power_or_color_dispatch(monkeypatch, space, bus, tool, arguments):
    from cloud.agents.lamp.tools.execute import execute_lamp_tool
    from cloud.esp32.physical_tasks import stable_task_id
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: space)
    monkeypatch.setattr("cloud.cards.registry.get_registry", lambda: SimpleNamespace(get_card=lambda _: {"unit_id": "esp32_desk_led"}))
    order = []
    def record(*args, **kwargs):
        order.append(("decision", args, kwargs))
        return {"event_id": "evt_decision"}
    bus[3].record_event.side_effect = record
    def physical(**kwargs):
        order.append(("physical", kwargs))
        return {"result": "ok"}
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    context = ExecutionContext("peer_game_color_invite123", "agent", False, "main", "agent_autonomous", int(time.time()+30))
    result = execute_lamp_tool(tool, {**arguments,
        "decision_reason": "它刚分享了白色玩偶，我选白色让它有东西可找。", "evidence_refs": ["evt_seen"]},
        "step", agent=SimpleNamespace(_state=None, _should_act=lambda: True, _device_online=lambda: True),
        unit_id="esp32_desk_led", context=context, dispatch=Mock(), lighting_only=True, run_id="run")
    assert result["result"] == "ok"
    assert [entry[0] for entry in order] == ["decision", "physical"]
    decision = order[0][2]
    assert order[0][1] == ("act_test", "action")
    assert decision["decision_basis"]["reason"].startswith("它刚分享")
    assert decision["action"] == tool and decision["status"] == "executing"
    assert decision["task_id"] == stable_task_id("esp32_desk_led", "run", "step")
    assert decision["trigger_message_id"] == "invite123"
    assert "准备" in decision["text"] and "已" not in decision["text"]


def test_user_hold_blocks_game_decision_and_dispatch(monkeypatch, space, bus):
    from cloud.agents.lamp.tools.execute import execute_lamp_tool
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: space)
    monkeypatch.setattr("cloud.cards.registry.get_registry", lambda: SimpleNamespace(get_card=lambda _: {"unit_id": "esp32_desk_led"}))
    physical = Mock()
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    context = ExecutionContext("peer_game_color_test", "agent", False, "main", "agent_autonomous", int(time.time()+30))
    result = execute_lamp_tool("set_led_color", {"r": 255, "g": 255, "b": 255,
        "decision_reason": "它分享了白色玩偶，我想选白色。", "evidence_refs": ["evt_seen"]},
        "step", agent=SimpleNamespace(_state=None, _should_act=lambda: False, _device_online=lambda: True),
        unit_id="esp32_desk_led", context=context, dispatch=Mock(), lighting_only=True, run_id="run")
    assert result["result"] == "blocked"
    bus[3].record_event.assert_not_called()
    physical.assert_not_called()
