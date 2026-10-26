"""Contract boundary: v1 validation, legacy reads and runtime projection."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from cloud.cards.builder import build_card_from_manifest, parse_card
from cloud.cards.registry import CardRegistry


LED_MANIFEST = {
    "unit_id": "esp32_desk_led", "parent_id": "esp32_desk",
    "name": "桌面灯", "agent_type": "actuator", "fsm": "led", "fsm_version": 2,
    "capabilities": [{"action": "SET_STATE"}, {"action": "SET_COLOR"}],
}


def card() -> dict:
    return build_card_from_manifest(LED_MANIFEST)


def test_builder_emits_valid_v1_and_registry_projects_status_and_state():
    built = card()
    assert built["card_schema_version"] == 1
    assert built["state_machine"]["state_field"] == "ism"
    registry = CardRegistry()
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "BRIGHT", "ts": 10}))
    registry.handle_message("ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST))
    assert registry.get_card("esp32_desk_led")["state"]["ism"] == "ON"
    assert registry.get_card("esp32_desk_led")["online"] is False
    registry.handle_message("ssm/agents/esp32_desk/status", "online")
    assert registry.get_card("esp32_desk_led")["online"] is True


def test_retained_state_is_reprojected_when_card_version_changes():
    registry = CardRegistry()
    topic = "ssm/agents/esp32_desk_led/manifest"
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "BRIGHT", "ts": 10}))
    registry.handle_message(topic, json.dumps(LED_MANIFEST))
    assert registry.get_card("esp32_desk_led")["state"]["ism"] == "ON"
    registry.handle_message(topic, json.dumps({**LED_MANIFEST, "fsm_version": 1}))
    assert registry.get_card("esp32_desk_led")["state"]["ism"] == "BRIGHT"


@pytest.mark.parametrize("mutate", [
    lambda c: c["state_machine"].pop("state_field"),
    lambda c: c["state_machine"]["transitions"].append({"src": "GONE", "dst": "ON", "trigger": "X", "label": "bad"}),
    lambda c: c["services"].update({"chat_endpoint": "https://outside.test/chat"}),
    lambda c: c["state_machine"]["views"]["OFF"].update({"type": "image", "src": "https://outside.test/view.png"}),
    lambda c: c.update({"card_schema_version": 2}),
])
def test_invalid_v1_does_not_replace_last_good_card(mutate):
    registry = CardRegistry()
    good = card()
    good["services"] = {"chat_endpoint": "/api/esp32/esp32_desk_led/chat"}
    topic = "ssm/agents/esp32_desk_led/card"
    registry.handle_message(topic, json.dumps(good))
    bad = deepcopy(good)
    mutate(bad)
    registry.handle_message(topic, json.dumps(bad))
    assert registry.get_card("esp32_desk_led")["state_machine"] == good["state_machine"]


def test_unknown_or_invalid_widget_is_skipped_without_dropping_card(caplog):
    payload = card()
    payload["widgets"].extend([
        {"type": "map", "states": []},
        {"type": "video", "states": [], "endpoint": "https://outside.test/video"},
        {"type": "joystick", "states": ["ON"]},
    ])
    parsed = parse_card(payload)
    assert [w["type"] for w in parsed["widgets"]] == ["led_color"]
    assert "Skipping" in caplog.text


def test_v0_retained_card_is_still_discoverable():
    legacy = {"slug": "legacy", "name": "旧设备", "agent_type": "robot",
              "transport": {"kind": "http"}, "skills": []}
    assert parse_card(legacy)["unit_id"] == "legacy"
    registry = CardRegistry()
    registry.handle_message("ssm/agents/legacy/card", json.dumps(legacy))
    assert registry.get_card("legacy") is not None


def test_missing_v1_identity_is_rejected():
    payload = card()
    payload.pop("unit_id")
    with pytest.raises(ValueError, match="unit_id"):
        parse_card(payload)


def test_connection_state_is_separate_from_mqtt_online():
    payload = card()
    payload["skills"] = [skill for skill in payload["skills"] if skill["id"] != "adjust_lighting"]
    payload["unit_id"] = payload["device_id"] = "robot"
    payload["state_machine"] = {
        "state_field": "fsm_state", "connection_field": "connected",
        "states": ["standing"], "transitions": [],
    }
    registry = CardRegistry()
    registry.handle_message("ssm/agents/robot/card", json.dumps(payload))
    registry.handle_message("ssm/agents/robot/status", "online")
    registry.handle_message("ssm/agents/robot/state", json.dumps({"fsm_state": "offline", "connected": False}))
    found = registry.get_card("robot")
    assert found["online"] is True
    assert found["state"]["connected"] is False


def test_lamp_skill_transport_override_cannot_target_another_service():
    payload = card()
    semantic = next(skill for skill in payload["skills"] if skill["id"] == "adjust_lighting")
    semantic["invoke"]["transport"]["endpoint"] = "https://outside.example/api/internal/secret"
    with pytest.raises(ValueError, match="same-origin"):
        parse_card(payload)
    semantic["invoke"]["transport"]["endpoint"] = "/api/internal/esp32/other/agent/tasks"
    with pytest.raises(ValueError, match="allowed lamp service"):
        parse_card(payload)


def test_card_json_view_is_grouped_but_round_trips():
    from cloud.api.devices import _readable_card

    payload = card()
    response = _readable_card(payload)
    assert response.media_type == "application/json"
    rendered = response.body.decode()
    assert "\n\n" in rendered
    assert "桌面灯" in rendered
    assert json.loads(rendered) == payload
    assert rendered.index('"unit_id"') < rendered.index('"transport"') < rendered.index('"skills"') < rendered.index('"state_machine"')


def test_device_sse_uses_registry_normalized_state(monkeypatch):
    from cloud.api import intents

    registry = CardRegistry()
    registry.handle_message("ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST))
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "BRIGHT", "ts": 10}))
    events = []
    monkeypatch.setattr(intents, "get_registry", lambda: registry)
    monkeypatch.setattr(intents, "get_space_registry", lambda: type("Space", (), {"get_space": lambda self: {"device_ids": ["esp32_desk"]}})())
    monkeypatch.setattr(intents._events, "broadcast", events.append)
    assert intents.handle_mqtt_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "BRIGHT", "ts": 10}))
    assert events[0]["state"]["ism"] == "ON"
