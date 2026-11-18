"""Regression checks for live MQTT receipts and retained state freshness."""

import json

from cloud.cards.registry import CardRegistry
from cloud.esp32.state import ESP32State
from cloud.orchestrator.shared_state import SharedState
from cloud.peers.view import _state_summary
from cloud.space.context import _summarize
from cloud.space.decisions import decision_evidence


def test_live_state_uses_cloud_receipt_even_when_edge_clock_is_wrong():
    now = 1793776280.0
    payload = {"unit_id": "esp32_desk_led", "ts": 4930,
               "r": 255, "g": 255, "b": 255, "brightness": 100,
               "_received_at": now - 1000}
    for state in (ESP32State(), SharedState()):
        state.on_manifest("esp32_desk_led", {"agent_type": "actuator"})
        state.on_agent_msg("esp32_desk_led", "state", payload, received_at=now)
        stored = state.actuator_snapshot()["esp32_desk_led"]
        assert stored["state"]["_received_at"] == now
        assert _summarize("esp32_desk_led", stored, now)["status"] == "current"
        assert _summarize("esp32_desk_led", stored, now + 61)["status"] == "stale"
        assert payload["_received_at"] == now - 1000


def test_retained_state_is_not_freshened_on_delivery():
    now = 1793776280.0
    topic = "ssm/agents/esp32_desk_led/state"
    payload = json.dumps({"ts": 4930, "ism": "ON", "_received_at": now})
    registry = CardRegistry()
    registry.handle_message(topic, payload, retained=True, received_at=now)
    retained = registry._states["esp32_desk_led"]
    assert "_received_at" not in retained
    assert _state_summary(retained, now)["status"] == "stale"
    registry.handle_message(topic, payload, retained=False, received_at=now)
    live = registry._states["esp32_desk_led"]
    assert live["_received_at"] == now
    assert _state_summary(live, now)["status"] == "current"
    assert _state_summary(live, now + 61)["status"] == "stale"


def test_confirmed_online_lighting_survives_long_game_without_refreshing_receipt():
    now = 1793776280.0
    state = {"ism": "ON", "r": 0, "g": 90, "b": 255, "brightness": 160,
             "_received_at": now, "ts": 4930}
    card = {"online": True, "state": state}
    later = now + 3600
    environment = _summarize("lamp", {"state": state}, later, card=card)
    peer = _state_summary(state, later, online=True)
    assert environment["status"] == peer["status"] == "current"
    assert environment["as_of"] == peer["as_of"] == state["_received_at"] == now
    evidence = decision_evidence({"environment": [environment], "peers": [
        {"unit_id": "lamp", "online": True, "state_summary": peer}]}, now=later)
    assert len(evidence) == 2
    assert all(item["validity"] == "until_changed" and item["as_of"] == now for item in evidence)
    sampled = _summarize("sensor", {"state": {"value": 20, "_received_at": now}}, later)
    assert sampled["status"] == "stale"


def test_lamp_reconnection_requires_new_physical_state_even_inside_age_window():
    now = 1793776280.0
    registry = CardRegistry()
    registry._cards["lamp"] = {"unit_id": "lamp", "device_id": "board"}
    payload = json.dumps({"ism": "ON", "r": 0, "g": 90, "b": 255,
                          "brightness": 160, "ts": now})
    registry.handle_message("ssm/agents/board/status", b"online")
    registry.handle_message("ssm/agents/lamp/state", payload, received_at=now)
    registry.handle_message("ssm/agents/board/status", b"offline")
    registry.handle_message("ssm/agents/board/status", b"online")
    card = registry.get_card("lamp")
    summary = _state_summary(card["state"], now + 1, online=card["online"])
    assert summary["status"] == "stale"
    local_state = {"state": json.loads(payload) | {"_received_at": now}}
    assert _summarize("lamp", local_state, now + 1, card=card)["status"] == "stale"
    registry.handle_message("ssm/agents/lamp/state", payload, received_at=now + 2)
    card = registry.get_card("lamp")
    assert _state_summary(card["state"], now + 3600, online=card["online"])["status"] == "current"
    registry.handle_message("ssm/agents/lamp/state", payload, retained=True, received_at=now + 3600)
    card = registry.get_card("lamp")
    assert _state_summary(card["state"], now + 3601, online=card["online"])["status"] == "stale"
