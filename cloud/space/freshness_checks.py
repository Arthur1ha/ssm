"""Regression checks for live MQTT receipts and retained state freshness."""

import json

from cloud.cards.registry import CardRegistry
from cloud.esp32.state import ESP32State
from cloud.orchestrator.shared_state import SharedState
from cloud.peers.view import _state_summary
from cloud.space.context import _summarize


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
