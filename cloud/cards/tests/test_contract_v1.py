"""Current Card validation, rejection of retired data and runtime projection."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from cloud.cards.builder import build_card_from_manifest
from cloud.cards.validation import normalize_card
from cloud.cards.tests.test_builder import LED_MANIFEST
from cloud.cards.registry import CardRegistry


def card() -> dict:
    return build_card_from_manifest(LED_MANIFEST)


def test_builder_emits_valid_v1_and_registry_projects_status_and_state():
    built = card()
    assert built["card_schema_version"] == 1
    assert built["state_machine"]["regions"][0]["state_field"] == "ism"
    registry = CardRegistry()
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "ON", "ts": 10}))
    registry.handle_message("ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST))
    assert registry.get_card("esp32_desk_led")["state"]["ism"] == "ON"
    assert registry.get_card("esp32_desk_led")["online"] is False
    registry.handle_message("ssm/agents/esp32_desk/status", "online")
    assert registry.get_card("esp32_desk_led")["online"] is True


def test_retired_manifest_does_not_replace_current_static_card():
    registry = CardRegistry()
    topic = "ssm/agents/esp32_desk_led/manifest"
    registry.handle_message(topic, json.dumps(LED_MANIFEST))
    original = registry.get_static_card("esp32_desk_led")
    registry.handle_message(topic, json.dumps({**LED_MANIFEST, "fsm_version": 2}))
    assert registry.get_static_card("esp32_desk_led") == original


@pytest.mark.parametrize("mutate", [
    lambda c: c["state_machine"]["regions"][0].pop("state_field"),
    lambda c: c["state_machine"]["regions"][0]["transitions"].append({"src": "GONE", "dst": "ON", "trigger": "X", "label": "bad"}),
    lambda c: c["services"].update({"chat_endpoint": "https://outside.test/chat"}),
    lambda c: c["state_machine"]["regions"][0]["views"]["OFF"].update({"type": "image", "src": "https://outside.test/view.png"}),
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
    parsed = normalize_card(payload)
    assert [w["type"] for w in parsed["widgets"]] == ["led_color"]
    assert "Skipping" in caplog.text


def test_map_widget_accepts_only_same_origin_api_endpoint():
    payload = card()
    payload["widgets"] = [{"type": "map", "states": [], "placement": "media",
                           "endpoint": "/api/go2/map.png"}]
    assert normalize_card(payload)["widgets"] == payload["widgets"]

    payload["widgets"][0]["endpoint"] = "https://outside.test/map.png"
    assert normalize_card(payload)["widgets"] == []


def test_v0_retained_card_is_rejected():
    payload = {"slug": "legacy", "name": "旧设备", "agent_type": "robot",
               "transport": {"kind": "http"}, "skills": []}
    with pytest.raises(ValueError, match="card_schema_version"):
        normalize_card(payload)
    registry = CardRegistry()
    registry.handle_message("ssm/agents/legacy/card", json.dumps(payload))
    assert registry.get_card("legacy") is None


def test_missing_v1_identity_is_rejected():
    payload = card()
    payload.pop("unit_id")
    with pytest.raises(ValueError, match="unit_id"):
        normalize_card(payload)


def test_connection_state_is_separate_from_mqtt_online():
    payload = card()
    payload["skills"] = [skill for skill in payload["skills"] if skill["id"] != "adjust_lighting"]
    payload["unit_id"] = payload["device_id"] = "robot"
    payload["state_machine"] = {
        "primary_region": "body", "connection_field": "connected",
        "regions": [{"id":"body", "label":"身体", "state_field":"body_state", "states":["standing"], "transitions":[]}],
    }
    registry = CardRegistry()
    registry.handle_message("ssm/agents/robot/card", json.dumps(payload))
    registry.handle_message("ssm/agents/robot/status", "online")
    registry.handle_message("ssm/agents/robot/state", json.dumps({"body_state": "offline", "connected": False}))
    found = registry.get_card("robot")
    assert found["online"] is True
    assert found["state"]["connected"] is False


def test_lamp_skill_transport_override_cannot_target_another_service():
    payload = card()
    semantic = next(skill for skill in payload["skills"] if skill["id"] == "adjust_lighting")
    semantic["invoke"]["transport"]["endpoint"] = "https://outside.example/api/internal/secret"
    with pytest.raises(ValueError, match="same-origin"):
        normalize_card(payload)
    semantic["invoke"]["transport"]["endpoint"] = "/api/internal/esp32/other/agent/tasks"
    with pytest.raises(ValueError, match="allowed lamp service"):
        normalize_card(payload)


def test_card_json_view_is_grouped_but_round_trips():
    from cloud.api.devices import _readable_card

    payload = card()
    response = _readable_card(payload)
    assert response.media_type == "application/json"
    rendered = response.body.decode()
    assert "\n\n" in rendered
    assert LED_MANIFEST["name"] in rendered
    assert json.loads(rendered) == payload
    assert rendered.index('"unit_id"') < rendered.index('"transport"') < rendered.index('"skills"') < rendered.index('"state_machine"')


def test_device_sse_uses_registry_normalized_state(monkeypatch):
    from cloud.api import intents

    registry = CardRegistry()
    registry.handle_message("ssm/agents/esp32_desk_led/manifest", json.dumps(LED_MANIFEST))
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "ON", "ts": 10}))
    events = []
    monkeypatch.setattr(intents, "get_registry", lambda: registry)
    monkeypatch.setattr(intents, "get_space_registry", lambda: type("Space", (), {"get_space": lambda self: {"device_ids": ["esp32_desk"]}})())
    monkeypatch.setattr(intents._events, "broadcast", events.append)
    assert intents.handle_mqtt_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "ON", "ts": 10}))
    assert events[0]["state"]["ism"] == "ON"


@pytest.mark.asyncio
async def test_agent_endpoint_returns_static_card_and_device_endpoint_keeps_runtime(monkeypatch):
    from cloud.api import devices

    registry = CardRegistry()
    declaration = card()
    registry.handle_message("ssm/agents/esp32_desk_led/card", json.dumps(declaration))
    registry.handle_message("ssm/agents/esp32_desk/status", "online")
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "ON", "ts": 10}))
    monkeypatch.setattr(devices, "get_registry", lambda: registry)
    monkeypatch.setattr(devices, "_fixed_cards", registry.get_all_cards)
    raw = await devices.device_agent_card("esp32_desk_led", _session={})
    snapshot = await devices.get_device("esp32_desk_led", _session={})
    assert json.loads(raw.body) == declaration
    assert json.loads(snapshot.body)["online"] is True
    assert json.loads(snapshot.body)["state"]["ism"] == "ON"


@pytest.mark.asyncio
async def test_agent_endpoint_does_not_fabricate_a_card_for_offline_placeholder(monkeypatch):
    from fastapi import HTTPException
    from cloud.api import devices

    monkeypatch.setattr(devices, "get_registry", CardRegistry)
    monkeypatch.setattr(devices, "_fixed_cards", lambda: {"go2": {"unit_id": "go2", "online": False}})
    with pytest.raises(HTTPException) as error:
        await devices.device_agent_card("go2", _session={})
    assert error.value.status_code == 404
