"""Shared declarative region boundary; supplemental to real-device acceptance."""

from copy import deepcopy
import json
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cloud.cards.builder import build_card_from_manifest
from cloud.cards.registry import CardRegistry
from cloud.cards.validation import normalize_card
from edge.lamp_contract import FSM_VERSION


def lamp(version=FSM_VERSION):
    return build_card_from_manifest({'firmware_ver': '0.3.0',
        "unit_id": "esp32_desk_led", "parent_id": "esp32_desk",
        "name": "桌面灯", "agent_type": "actuator", "fsm": "led", "fsm_version": version,
        "capabilities": [{"action": "SET_STATE"}, {"action": "SET_COLOR"}, {"action": "BLINK"}],
    })


def test_lamp_card_matches_three_state_edge_contract():
    from edge.ism import LED_TABLE
    machine = lamp()["state_machine"]
    assert set(machine) == {"primary_region", "regions"}
    region = machine["regions"][0]
    declared = {(edge["src"], edge["trigger"]): edge["dst"] for edge in region["transitions"]}
    for key, destination in LED_TABLE.items():
        assert declared[key] == destination
    assert region["states"] == ["OFF", "ON", "BLINKING"]
    assert set(region["views"]) == set(region["states"])
    assert all(not edge.get("action") for edge in region["transitions"] if edge["trigger"] == "BLINK_DONE")


def test_lamp_card_contains_only_one_static_declaration():
    card = lamp()
    region = card["state_machine"]["regions"][0]
    assert card["state_machine"]["primary_region"] == region["id"] == "device"
    assert region["state_field"] == "ism"
    assert card["modes"][0]["state_field"] == "autonomy_mode"
    assert not {"online", "state", "training_capabilities", "parent_id"}.intersection(card)
    assert "params_schema" not in card["widgets"][0]
    region["states"].append("TEST_ONLY")
    assert "TEST_ONLY" not in lamp()["state_machine"]["regions"][0]["states"]


@pytest.mark.parametrize("version", [None, 1, 2])
def test_retired_firmware_is_rejected(version):
    with pytest.raises(ValueError, match="unsupported lamp fsm_version"):
        lamp(version)


def test_registry_keeps_static_card_separate_from_runtime_and_returns_detached_data():
    registry = CardRegistry()
    declaration = lamp()
    base = "ssm/agents/esp32_desk_led/"
    registry.handle_message(base + "card", json.dumps(declaration))
    registry.handle_message("ssm/agents/esp32_desk/status", "online")
    registry.handle_message(base + "state", json.dumps({"ism": "ON", "ts": 2}))
    snapshot = registry.get_card("esp32_desk_led")
    assert snapshot["online"] is True and snapshot["state"]["ism"] == "ON"
    assert registry.get_static_card("esp32_desk_led") == declaration
    snapshot["state_machine"]["regions"][0]["states"].clear()
    assert registry.get_static_card("esp32_desk_led") == declaration


@pytest.mark.parametrize("mutation", [
    lambda machine: machine.update(primary_region="missing"),
    lambda machine: machine.update(primary_region=[]),
    lambda machine: machine.update(execution_field=""),
    lambda machine: machine["regions"][0].update(available_actions_field=[]),
    lambda machine: machine["regions"][0]["transitions"][0].update(required_actor="root"),
    lambda machine: machine["regions"][0]["transitions"][0].update(action="", availability_key="not-a-command"),
])
def test_invalid_shared_presentation_metadata_rejects_card(mutation):
    card = lamp()
    mutation(card["state_machine"])
    with pytest.raises(ValueError):
        normalize_card(card)


def test_widget_binding_uses_declared_region_and_simple_value_conditions():
    card = lamp()
    widget = {"type": "joystick", "action": "velocity", "state_field": "ism",
              "states": ["ON"], "enabled_when": {"ism": ["ON", "BLINKING"]},
              "available_actions_field": "available_actions", "availability_key": "Move"}
    card["widgets"] = [widget]
    assert normalize_card(card)["widgets"] == [widget]
    nested = deepcopy(card)
    nested["widgets"][0]["enabled_when"] = {"control_state": {"expression": "eval"}}
    assert normalize_card(nested)["widgets"] == []
    unknown = deepcopy(card)
    unknown["widgets"][0]["state_field"] = "unregistered_state"
    assert normalize_card(unknown)["widgets"] == []


def test_go2_declares_task_events_and_one_body_topology():
    from cloud.go2.card import _go2_state_machine
    machine = _go2_state_machine()
    regions = {region["id"]: region for region in machine["regions"]}
    assert regions[machine["primary_region"]]["state_field"] == "body_state"
    assert machine["execution_field"] == "pending_action"
    assert regions["body"]["available_actions_field"] == "available_actions"
    automatic = {(edge["src"], edge["dst"]) for edge in regions["life"]["transitions"] if not edge.get("action")}
    assert {("idle", "play"), ("play", "idle"), ("idle", "explore"), ("explore", "idle")} <= automatic
    permission_stop = next(edge for edge in regions["life"]["transitions"]
                           if edge.get("availability_key") == "stop_explore" and edge["src"] == "idle")
    assert permission_stop["dst"] == "idle" and permission_stop["available_only"] is True
    assert not {"state_field", "states", "transitions", "views", "layout"}.intersection(machine)


def test_mqtt_projection_preserves_execution_and_observation_evidence(monkeypatch):
    from cloud.go2 import card
    snapshot = {"last_action": {"command": "Hello", "result": "ok", "confirmation": "posture"},
                "body_observation_source": "sport", "body_observed_at": 10,
                "body_telemetry_fresh": True, "exploration_enabled": False,
                "last_transition": {"event": "body_observation"}, "ts": 10}
    monkeypatch.setattr(card, "go2", SimpleNamespace(state_snapshot=lambda: snapshot))
    mqtt = MagicMock()
    monkeypatch.setattr(card, "_mqtt_client", mqtt)
    card._publish_go2_state()
    published = json.loads(mqtt.publish.call_args.args[1])
    assert all(published[key] == value for key, value in snapshot.items())


def test_go2_whiteboard_groups_do_not_invent_pose_states():
    from cloud.go2.card import _build_go2_card
    machine = normalize_card(_build_go2_card())['state_machine']
    regions = {region['id']: region for region in machine['regions']}
    assert 'unknown' not in regions['body']['states'] and 'unknown' not in regions['body']['views']
    assert regions['activity']['diagram_group'] == regions['life']['diagram_group'] == '行为'
    assert regions['body']['diagram_group'] == '姿态与动作'
    assert regions['body']['node_radius'] < regions['life']['node_radius']
    assert set(regions['body']['layout']) == set(regions['body']['states'])


@pytest.mark.parametrize('diagram', [
    {'width': 0, 'height': 720, 'sections': []},
    {'width': 600, 'height': float('nan'), 'sections': []},
    {'width': 600, 'height': 720, 'sections': [{'label': '行为', 'top': 900}]},
])
def test_reject_malformed_whiteboard_dimensions(diagram):
    from cloud.go2.card import _build_go2_card
    card = _build_go2_card()
    card['state_machine']['diagram'] = diagram
    with pytest.raises(ValueError):
        normalize_card(card)


def test_go2_all_actions_fit_and_contextual_joystick_contract():
    from cloud.go2.card import _build_go2_card
    card = normalize_card(_build_go2_card())
    machine = card['state_machine']
    body = next(region for region in machine['regions'] if region['id'] == 'body')
    layout = body['layout']
    assert len({point['y'] for point in layout.values()}) > 2
    assert layout['standing'] == {'x': .5, 'y': 475 / 670}
    assert any(point['x'] < .5 for point in layout.values())
    assert any(point['x'] > .5 for point in layout.values())
    assert any(point['y'] < layout['standing']['y'] for point in layout.values())
    assert any(point['y'] > layout['standing']['y'] for point in layout.values())
    regions = {region['id']: region for region in machine['regions']}
    rest = regions['activity']['layout']['rest']
    idle, explore, play = (regions['life']['layout'][key] for key in ('idle', 'explore', 'play'))
    assert rest['y'] == idle['y'] < explore['y'] == play['y']
    assert rest['x'] == explore['x'] < idle['x'] == play['x']
    for point in layout.values():
        assert 0 <= point['x'] <= 1 and 0 <= point['y'] <= 1
        radius = body['node_radius'] + 7
        x, y = point['x']*machine['diagram']['width'], point['y']*machine['diagram']['height']
        assert radius <= x <= machine['diagram']['width']-radius
        assert radius <= y <= machine['diagram']['height']-radius
    joystick = next(widget for widget in card['widgets'] if widget['type'] == 'joystick')
    assert joystick['node_state'] == 'moving'
    assert joystick['enter_action'] == 'EnterMoveControl'
    assert joystick['exit_action'] == 'StopMove'


@pytest.mark.parametrize('field,value', [('node_state', 'missing'), ('enter_action', ''), ('exit_action', 'not_declared')])
def test_reject_invalid_contextual_joystick(field, value):
    from cloud.go2.card import _build_go2_card
    card = _build_go2_card()
    widget = next(item for item in card['widgets'] if item['type'] == 'joystick')
    widget[field] = value
    assert all(item['type'] != 'joystick' for item in normalize_card(card)['widgets'])
