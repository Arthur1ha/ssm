import json

from cloud.cards.registry import CardRegistry
from cloud.peers.view import list_peers
from cloud.space.registry import SpaceRegistry, get_space_cards


def _card(unit_id, device_id, agent_type, skill_id):
    return {
        "unit_id": unit_id,
        "device_id": device_id,
        "name": unit_id,
        "description": "",
        "agent_type": agent_type,
        "online": True,
        "transport": {"kind": "mqtt"},
        "skills": [{
            "id": skill_id, "name": skill_id, "tags": ["public"],
            "params_schema": {"type": "object"}, "invoke": {"action": skill_id},
        }],
        "state": {},
    }


def _publish_card(registry, card):
    registry.handle_message(f"ssm/agents/{card['unit_id']}/card", json.dumps(card))


def test_peer_view_requires_space_membership_and_keeps_decision_internal(tmp_path):
    space = SpaceRegistry(tmp_path / "space.json")
    space.update_space(device_ids=["esp32_desk", "go2"])
    registry = CardRegistry()
    light = _card("esp32_desk_led", "esp32_desk", "actuator", "set_state")
    go2 = _card("go2", "go2", "robot", "go2_sport")
    decision = _card("cloud_orchestrator", "cloud_orchestrator", "decision", "coordinate_request")
    outsider = _card("other_led", "other", "actuator", "set_state")
    for card in (light, go2, decision, outsider):
        _publish_card(registry, card)
    for device_id in ("esp32_desk", "go2", "cloud_orchestrator", "other"):
        registry.handle_message(f"ssm/agents/{device_id}/status", "online")

    peers = list_peers(registry, space, initiator_unit_id="go2")
    assert {peer["unit_id"] for peer in peers} == {"esp32_desk_led", "cloud_orchestrator"}
    assert all(peer["online"] for peer in peers)
    assert "cloud_orchestrator" not in get_space_cards(registry.get_all_cards(), space)
    assert list_peers(registry, space, initiator_unit_id="other_led") == []
    assert {peer["unit_id"] for peer in list_peers(
        registry, space, initiator_unit_id="cloud_orchestrator"
    )} == {"esp32_desk_led", "go2"}


def test_status_is_authoritative_and_state_staleness_is_explicit(tmp_path):
    space = SpaceRegistry(tmp_path / "space.json")
    space.update_space(device_ids=["esp32_desk", "go2"])
    registry = CardRegistry()
    _publish_card(registry, _card("go2", "go2", "robot", "go2_sport"))
    _publish_card(registry, _card("esp32_desk_led", "esp32_desk", "actuator", "set_state"))
    registry.handle_message("ssm/agents/go2/status", "online")
    registry.handle_message("ssm/agents/esp32_desk_led/state", json.dumps({
        "unit_id": "esp32_desk_led", "ism": "ON", "ts": 1000,
    }))
    stale = list_peers(registry, space, initiator_unit_id="go2", now=1100)[0]
    assert stale["online"] is False
    assert stale["state_summary"] == {"status": "stale", "as_of": 1000.0}

    registry.handle_message("ssm/agents/esp32_desk/status", "online")
    fresh = list_peers(registry, space, initiator_unit_id="go2", now=1010)[0]
    assert fresh["online"] is True
    assert fresh["state_summary"]["ism"] == "ON"
    registry.handle_message("ssm/agents/esp32_desk/status", "offline")
    assert list_peers(registry, space, initiator_unit_id="go2", now=1010)[0]["online"] is False


def test_card_topic_identity_must_match_payload():
    registry = CardRegistry()
    _publish_card(registry, _card("go2", "go2", "robot", "go2_sport"))
    spoof = _card("cloud_orchestrator", "cloud_orchestrator", "decision", "coordinate_request")
    registry.handle_message("ssm/agents/go2/card", json.dumps(spoof))
    assert registry.get_card("cloud_orchestrator") is None
    assert registry.get_card("go2") is not None


def test_legacy_decision_manifest_does_not_clear_retained_card():
    registry = CardRegistry()
    decision = _card("cloud_orchestrator", "cloud_orchestrator", "decision", "coordinate_request")
    _publish_card(registry, decision)
    registry.handle_message("ssm/agents/cloud_orchestrator/status", "online")
    registry.handle_message("ssm/agents/cloud_orchestrator/manifest", json.dumps({
        "unit_id": "cloud_orchestrator", "agent_type": "decision", "hw_platform": "cloud",
    }))
    assert registry.get_card("cloud_orchestrator")["online"] is True
