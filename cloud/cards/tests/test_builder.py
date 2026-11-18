"""Current static manifest/Card contract; retired formats are rejected."""
from copy import deepcopy
import json

import pytest

from cloud.cards.builder import build_card_from_manifest
from cloud.cards.registry import CardRegistry
from cloud.cards.validation import normalize_card
from edge.lamp_contract import capabilities, FSM_VERSION

LED_MANIFEST = {
    "unit_id": "esp32_desk_led", "parent_id": "esp32_desk", "agent_type": "actuator",
    "name": "ws2812_ring", "firmware_ver": "0.3.0", "fsm_version": FSM_VERSION,
    "capabilities": capabilities(), "tags": ["lighting", "ambiance"],
}
GO2_CARD = {
    "card_schema_version": 1, "version": "2", "unit_id": "go2", "device_id": "go2",
    "name": "Go2 机器狗", "description": "四足机器人", "agent_type": "robot",
    "transport": {"kind": "http", "endpoint": "/api/go2/chat"}, "skills": [],
}


def test_manifest_preserves_identity_transport_and_public_skills():
    card = build_card_from_manifest(LED_MANIFEST)
    assert (card["unit_id"], card["device_id"], card["name"], card["version"]) == (
        "esp32_desk_led", "esp32_desk", "ws2812_ring", "0.3.0")
    assert card["tags"] == ["lighting", "ambiance"]
    assert card["transport"] == {"kind": "mqtt", "task_topic": "ssm/task/esp32_desk_led/{task_id}"}
    assert {skill["id"] for skill in card["skills"]} == {
        "set_light_state", "set_light_color", "blink_light", "adjust_lighting"}
    for capability in capabilities():
        skill = next(s for s in card["skills"] if s["invoke"]["action"] == capability["action"])
        assert skill["params_schema"] == capability["params_schema"]
    assert normalize_card(card) == card
    assert not {"state", "online", "training_capabilities", "parent_id"}.intersection(card)


def test_lamp_ui_and_topology_have_one_current_declaration():
    card = build_card_from_manifest(LED_MANIFEST)
    machine = card["state_machine"]
    assert set(machine) == {"primary_region", "regions"}
    assert machine["regions"][0]["states"] == ["OFF", "ON", "BLINKING"]
    assert card["modes"][0]["set_action"] == "set_mode"
    assert "set" not in card["modes"][0]
    assert card["widgets"][0]["states"] == ["ON", "BLINKING"]
    assert "params_schema" not in card["widgets"][0]


def test_new_unit_uses_its_declared_capabilities_without_name_guessing():
    card = build_card_from_manifest({**LED_MANIFEST, "unit_id": "another_actuator", "name": "其他单元"})
    assert card["state_machine"]["regions"][0]["state_field"] == "ism"
    assert card["transport"]["task_topic"] == "ssm/task/another_actuator/{task_id}"


def test_unknown_actions_are_not_advertised():
    manifest = deepcopy(LED_MANIFEST)
    manifest["capabilities"].append({"action": "UNKNOWN"})
    assert build_card_from_manifest(manifest) == build_card_from_manifest(LED_MANIFEST)


@pytest.mark.parametrize("version", [None, 1, 2])
def test_retired_lamp_firmware_is_rejected(version):
    with pytest.raises(ValueError, match="unsupported lamp fsm_version"):
        build_card_from_manifest({**LED_MANIFEST, "fsm_version": version})


def test_manifest_replaces_the_previous_declaration():
    registry = CardRegistry()
    topic = "ssm/agents/esp32_desk_led/manifest"
    registry.handle_message(topic, json.dumps(LED_MANIFEST))
    updated = {**LED_MANIFEST, "name": "updated", "capabilities": [{"action": "SET_STATE"}]}
    registry.handle_message(topic, json.dumps(updated))
    card = registry.get_static_card("esp32_desk_led")
    assert card["name"] == "updated"
    assert [s["id"] for s in card["skills"]] == ["set_light_state"]
    assert "widgets" not in card


def test_self_described_card_has_no_legacy_fallback():
    assert normalize_card(GO2_CARD) == GO2_CARD
    payload = {**GO2_CARD, "slug": "go2"}
    payload.pop("unit_id")
    with pytest.raises(ValueError, match="unit_id"):
        normalize_card(payload)
