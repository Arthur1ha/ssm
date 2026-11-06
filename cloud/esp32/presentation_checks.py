"""Cloud lamp presentation never changes or republishes physical edge facts."""

import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cloud.esp32 import presentation, service


@pytest.fixture
def control(monkeypatch):
    clock = [100.0]
    agent = service.ESP32Agent.__new__(service.ESP32Agent)
    agent._autonomy_mode = "manual"
    agent._user_hold_until = 0.0
    agent._control_view_lock = threading.Lock()
    agent._control_view_signature = None
    agent._control_view_ts = 0.0
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(service, "get_agent", lambda: agent)
    return agent, clock


def test_control_projection_separates_configuration_hold_and_expiration(control):
    agent, clock = control
    initial = agent.control_snapshot()
    clock[0] = 110
    assert agent.control_snapshot() == initial
    agent._autonomy_mode = "reactive"
    ready = agent.control_snapshot()
    assert ready["control_state"] == "ready"
    agent._user_hold_until = 120
    holding = agent.control_snapshot()
    assert holding["control_ts"] > ready["control_ts"]
    assert {key: value for key, value in holding.items() if key != "control_ts"} == {
        "autonomy_mode": "reactive", "control_state": "holding",
        "control_hold_until": 120,
    }
    clock[0] = 120
    assert agent.control_snapshot() == {
        "autonomy_mode": "reactive", "control_state": "ready",
        "control_hold_until": None, "control_ts": 120,
    }
    agent._autonomy_mode = "manual"
    assert agent.control_snapshot()["control_state"] == "manual"


def test_api_projection_preserves_edge_state_and_does_not_mutate_registry(control):
    card = {"unit_id": "esp32_desk_led", "online": False,
            "state": {"ism": "BLINKING", "brightness": 42, "ts": 9}}
    original = deepcopy(card)
    projected = presentation.project_card(card)
    assert card == original
    assert projected["state"]["ism"] == "BLINKING"
    assert projected["state"]["brightness"] == 42
    assert projected["state"]["ts"] == 9
    assert projected["state"]["control_state"] == "manual"
    assert projected["online"] is False
    assert presentation.project_card({"unit_id": "go2"}) == {"unit_id": "go2"}


def test_control_expiration_emits_once_without_physical_state_frame(control, monkeypatch):
    from cloud.api import intents
    from cloud.cards import registry
    from cloud.space import registry as spaces
    agent, clock = control
    card = {"unit_id": "esp32_desk_led", "device_id": "esp32_desk",
            "state": {"ism": "ON", "ts": 5}}
    monkeypatch.setattr(registry, "get_registry", lambda: SimpleNamespace(get_card=lambda _: card))
    monkeypatch.setattr(spaces, "get_space_registry", lambda: SimpleNamespace(
        get_space=lambda: {"device_ids": ["esp32_desk"]}))
    events = MagicMock()
    monkeypatch.setattr(intents, "_events", events)
    agent._autonomy_mode = "reactive"
    agent._user_hold_until = 102
    previous = presentation.publish_control_change(None)
    assert previous["control_state"] == "holding"
    presentation.publish_control_change(previous)
    assert events.broadcast.call_count == 1
    clock[0] = 102
    previous = presentation.publish_control_change(previous)
    presentation.publish_control_change(previous)
    assert events.broadcast.call_count == 2
    state = events.broadcast.call_args.args[0]["state"]
    assert state["control_state"] == "ready"
    assert state["ism"] == "ON" and state["ts"] == 5
