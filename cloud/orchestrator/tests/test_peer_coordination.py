"""Signed peer coordination stays separate from browser intents."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import graph
import tools
from trusted_context import (TrustedContextError, parse_trusted_peer_cancel,
                             parse_trusted_peer_request)
from cloud.mqtt_signing import NonceReplayGuard, sign_message


TOKEN = "service-secret-123456"


@pytest.fixture(autouse=True)
def configured_space(monkeypatch):
    monkeypatch.setattr("cloud.orchestrator.agentcore.tools.executor.get_space_registry",
                        lambda: MagicMock(get_space=lambda: {"device_ids": ["esp32_desk", "go2"]}))


def _signed_peer(*, origin_kind="user_delegated", **updates):
    body = {
        "request_id": "req_a", "initiator_unit_id": "go2",
        "target_unit_id": "cloud_orchestrator", "skill_id": "coordinate_request",
        "params": {"message": "请先开灯再打招呼"},
        "origin_kind": origin_kind,
        "origin_session_id": "user_a" if origin_kind == "user_delegated" else None,
        "actor": "visitor" if origin_kind == "user_delegated" else "agent",
        "persist": False, "space_id": "main", "issued_at": 1000,
        "expires_at": 1060,
    }
    body.update(updates)
    return sign_message("ssm/peer/request/req_a", body, internal_token=TOKEN, now=1000)


def test_peer_request_requires_signed_bound_context_and_fresh_nonce():
    guard = NonceReplayGuard()
    signed = _signed_peer()
    trusted = parse_trusted_peer_request(signed, "req_a", token=TOKEN,
                                         now=1000, replay_guard=guard)
    assert trusted["actor"] == "visitor"
    assert trusted["persist"] is False
    with pytest.raises(TrustedContextError, match="replayed"):
        parse_trusted_peer_request(signed, "req_a", token=TOKEN,
                                   now=1000, replay_guard=guard)
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_request(signed, "req_b", token=TOKEN, now=1000)
    signed["actor"] = "admin"
    with pytest.raises(TrustedContextError, match="signature"):
        parse_trusted_peer_request(signed, "req_a", token=TOKEN, now=1000)


def test_peer_cancellation_requires_fresh_signed_request():
    topic = "ssm/peer/cancel/req_a"
    payload = {"request_id": "req_a", "reason": "user_takeover", "ts": 1000}
    signed = sign_message(topic, payload, internal_token=TOKEN, now=1000)
    guard = NonceReplayGuard()
    assert parse_trusted_peer_cancel(signed, "req_a", token=TOKEN, now=1000,
                                     replay_guard=guard)["reason"] == "user_takeover"
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_cancel(signed, "req_a", token=TOKEN, now=1000,
                                  replay_guard=guard)
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_cancel(signed, "req_b", token=TOKEN, now=1000)


@pytest.mark.parametrize("updates", [
    {"persist": True},
    {"space_id": "other"},
    {"origin_session_id": None},
    {"expires_at": 999},
    {"params": {"message": ""}},
])
def test_peer_request_rejects_invalid_scope(updates):
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_request(_signed_peer(**updates), "req_a",
                                   token=TOKEN, now=1000)


def test_autonomous_context_cannot_claim_delegated_role():
    accepted = parse_trusted_peer_request(_signed_peer(origin_kind="agent_autonomous"),
                                          "req_a", token=TOKEN, now=1000)
    assert accepted["actor"] == "agent"
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_request(
            _signed_peer(origin_kind="agent_autonomous", actor="admin"),
            "req_a", token=TOKEN, now=1000,
        )


def _card(unit_id, kind, skill_id, tags, action=""):
    property_name = "state" if kind == "mqtt" else "cmd"
    return {"unit_id": unit_id, "device_id": "esp32_desk" if unit_id == "esp32_desk_led" else unit_id,
            "online": True,
            "transport": {"kind": kind, "endpoint": "/api/go2/chat"},
            "skills": [{"id": skill_id, "tags": tags,
                        "params_schema": {"type": "object", "required": [property_name],
                                          "properties": {property_name: {"type": "string"}}},
                        "invoke": {"action": action}}]}


def test_peer_dispatch_stops_before_next_action_when_first_fails(monkeypatch):
    cards = {
        "esp32_desk_led": _card("esp32_desk_led", "mqtt", "set_light_state", ["light"], "SET_STATE"),
        "go2": _card("go2", "http", "go2_sport", ["motion"], "SPORT"),
    }
    monkeypatch.setattr(tools, "_registry", MagicMock(get_card=lambda uid: cards.get(uid)))
    monkeypatch.setattr(tools, "_state", MagicMock(get_task_result=lambda *_: {"result": "blocked"}))
    publish = MagicMock()
    dispatch = MagicMock(return_value={"result": "ok"})
    monkeypatch.setattr(tools, "do_publish_task", publish)
    monkeypatch.setattr(tools, "do_http_dispatch", dispatch)
    state = {
        "session_id": "peer_req_a", "actor": "visitor", "persist": False,
        "space_id": "main", "origin_kind": "user_delegated",
        "expires_at": __import__("time").time() + 30,
        "user_msg": "开灯然后打招呼", "task_results": {},
        "planned_tasks": [
            {"unit_id": "esp32_desk_led", "skill_id": "set_light_state", "task_id": "a", "params": {"state": "ON"}},
            {"unit_id": "go2", "skill_id": "go2_sport", "task_id": "b", "params": {"cmd": "Hello"}},
        ],
    }
    result = graph._dispatch_peer_tasks_sequential(state)
    assert result["task_results"]["a"]["result"] == "blocked"
    assert result["task_results"]["b"]["result"] == "cancelled"
    publish.assert_called_once()
    dispatch.assert_not_called()


def test_peer_dispatch_waits_for_lamp_before_go2_and_preserves_delegation(monkeypatch):
    cards = {
        "esp32_desk_led": _card("esp32_desk_led", "mqtt", "set_light_state", ["light"], "SET_STATE"),
        "go2": _card("go2", "http", "go2_sport", ["motion"], "SPORT"),
    }
    monkeypatch.setattr(tools, "_registry", MagicMock(get_card=lambda uid: cards.get(uid)))
    events = []
    monkeypatch.setattr(tools, "_state", MagicMock(get_task_result=lambda *_: events.append("lamp_result") or {"result": "ok"}))
    monkeypatch.setattr(tools, "do_publish_task", lambda *_, **__: events.append("lamp_publish"))

    def dispatch(_, body, _timeout):
        events.append("go2_dispatch")
        assert body["session_id"] == "user-a"
        assert body["actor"] == "visitor"
        assert body["persist"] is False
        assert body["origin_kind"] == "user_delegated"
        return {"result": "ok"}

    monkeypatch.setattr(tools, "do_http_dispatch", dispatch)
    state = {
        "session_id": "peer_req_a", "origin_session_id": "user-a",
        "actor": "visitor", "persist": False, "space_id": "main",
        "origin_kind": "user_delegated", "expires_at": __import__("time").time() + 30,
        "user_msg": "开灯然后打招呼", "task_results": {},
        "planned_tasks": [
            {"unit_id": "esp32_desk_led", "skill_id": "set_light_state", "task_id": "a", "params": {"state": "ON"}},
            {"unit_id": "go2", "skill_id": "go2_sport", "task_id": "b", "params": {"cmd": "Hello"}},
        ],
    }
    result = graph._dispatch_peer_tasks_sequential(state)
    assert events == ["lamp_publish", "lamp_result", "go2_dispatch"]
    assert [result["task_results"][key]["result"] for key in ("a", "b")] == ["ok", "ok"]


@pytest.mark.parametrize("first_result, expected", [
    ("ok", ["adjust_lighting", "go2_sport"]),
    ("executing", ["adjust_lighting"]),
    ("timeout", ["adjust_lighting"]),
    ("blocked", ["adjust_lighting"]),
])
def test_semantic_lamp_must_finish_before_go2_dance(monkeypatch, first_result, expected):
    lamp = {"unit_id": "esp32_desk_led", "device_id": "esp32_desk", "online": True,
            "transport": {"kind": "http", "endpoint": "/api/internal/esp32/esp32_desk_led/agent/tasks"},
            "skills": [{"id": "adjust_lighting", "tags": ["light"],
                        "params_schema": {"type": "object", "required": ["goal"],
                                          "properties": {"goal": {"type": "string"}}},
                        "invoke": {"action": "adjust_lighting"}}]}
    dog = _card("go2", "http", "go2_sport", ["motion"], "SPORT")
    monkeypatch.setattr(tools, "_registry", MagicMock(get_card=lambda uid: {
        "esp32_desk_led": lamp, "go2": dog,
    }.get(uid)))
    calls = []
    def dispatch(endpoint, body, timeout):
        calls.append(body["skill_id"])
        return {"result": first_result if body["skill_id"] == "adjust_lighting" else "ok"}
    monkeypatch.setattr(tools, "do_http_dispatch", dispatch)
    state = {
        "session_id": "peer_req", "origin_session_id": "user-a", "actor": "visitor",
        "persist": False, "space_id": "main", "origin_kind": "user_delegated",
        "expires_at": __import__("time").time() + 30, "user_msg": "先调柔和再跳舞",
        "task_results": {}, "planned_tasks": [
            {"unit_id": "esp32_desk_led", "skill_id": "adjust_lighting", "task_id": "lamp_a",
             "params": {"goal": "柔和一点"}},
            {"unit_id": "go2", "skill_id": "go2_sport", "task_id": "dance_b",
             "params": {"cmd": "Dance1"}},
        ],
    }
    result = graph._dispatch_peer_tasks_sequential(state)
    assert calls == expected
    assert result["task_results"]["lamp_a"]["result"] == first_result
    assert result["task_results"]["dance_b"]["result"] == ("ok" if first_result == "ok" else "cancelled")


def test_dispatch_rejects_card_outside_configured_space(monkeypatch):
    from cloud.orchestrator.agentcore.tools import executor
    card = _card("esp32_desk_led", "mqtt", "set_light_state", ["light"], "SET_STATE")
    card["device_id"] = "neighbor_space_device"
    monkeypatch.setattr(executor, "get_space_registry", lambda: MagicMock(
        get_space=lambda: {"device_ids": ["esp32_desk", "go2"]},
    ))
    with pytest.raises(PermissionError, match="target_outside_space"):
        executor.validate_public_skill(card, card["skills"][0], {"state": "ON"},
                                       {"space_id": "main", "actor": "admin", "persist": False})


def test_autonomous_coordination_denies_go2_motion_other_than_hello(monkeypatch):
    card = _card("go2", "http", "go2_sport", ["motion"], "SPORT")
    monkeypatch.setattr(tools, "_registry", MagicMock(get_card=lambda _: card))
    dispatch = MagicMock()
    monkeypatch.setattr(tools, "do_http_dispatch", dispatch)
    state = {
        "session_id": "peer_req_a", "actor": "agent", "persist": False,
        "space_id": "main", "origin_kind": "agent_autonomous",
        "expires_at": __import__("time").time() + 30,
        "user_msg": "打招呼", "task_results": {},
        "planned_tasks": [{"unit_id": "go2", "skill_id": "go2_sport",
                           "task_id": "a", "params": {"cmd": "Dance1"}}],
    }
    result = graph._dispatch_peer_tasks_sequential(state)
    assert result["task_results"]["a"]["result"] == "blocked"
    dispatch.assert_not_called()


def test_user_takeover_cancels_undispatched_peer_task(monkeypatch):
    publish = MagicMock()
    monkeypatch.setattr(tools, "do_publish_task", publish)
    state = {
        "session_id": "peer_cancel", "peer_request_id": "cancel_me",
        "actor": "agent", "persist": False, "space_id": "main",
        "origin_kind": "agent_autonomous", "expires_at": __import__("time").time() + 30,
        "user_msg": "开灯", "task_results": {},
        "planned_tasks": [{"unit_id": "esp32_desk_led", "skill_id": "set_light_state",
                           "task_id": "a", "params": {"state": "ON"}}],
    }
    graph.cancel_peer_coordination("cancel_me")
    try:
        result = graph._dispatch_peer_tasks_sequential(state)
        assert result["task_results"]["a"]["result"] == "cancelled"
        publish.assert_not_called()
    finally:
        graph.clear_peer_coordination("cancel_me")


def test_peer_runner_keeps_feedback_private_and_does_not_upgrade_accepted(monkeypatch):
    now = __import__("time").time()
    mqtt = MagicMock()
    state_store = MagicMock()
    monkeypatch.setattr(tools, "_mqtt", mqtt)
    monkeypatch.setattr(tools, "_state", state_store)

    def invoke(graph_input):
        tools.do_publish_feedback(graph_input["session_id"], "ack", "开始执行")
        return {**graph_input,
                "planned_tasks": [{"task_id": "one"}],
                "task_results": {"one": {"result": "accepted"}},
                "response_text": "已发出，尚未确认完成。"}

    request = {
        "request_id": "req-a", "initiator_unit_id": "go2",
        "target_unit_id": "cloud_orchestrator", "actor": "visitor",
        "origin_kind": "user_delegated", "origin_session_id": "user-a",
        "space_id": "main", "issued_at": now, "expires_at": now + 30,
        "params": {"message": "请打招呼"},
    }
    result = graph.run_peer_coordination(MagicMock(invoke=invoke), request)
    assert result["result"] == "executing"
    mqtt.publish.assert_not_called()
    state_store.register_session.assert_called_once()
    state_store.clear_session.assert_called_once_with("peer_req-a")
