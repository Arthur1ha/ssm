"""Reactive and device requests share completion and FSM rules."""

from unittest.mock import MagicMock

from cloud.esp32.service import ESP32Agent
from cloud.esp32.tool_registry import TOOL_DECLARATIONS
from cloud.agents.lamp.memory import taught


def _agent(monkeypatch, *, ism="OFF", online=True):
    state = MagicMock()
    state.actuator_snapshot.return_value = {
        "esp32_desk_led": {"state": {"ism": ism, "brightness": 120}},
    }
    state.wait_task_result.return_value = {"result": "ok"}
    card = {"online": online, "state_machine": {"transitions": [
        {"src": "OFF", "action": "SET_STATE", "params": {"state": "ON"}},
    ]}}
    from cloud.cards import registry
    monkeypatch.setattr(registry, "get_registry", lambda: MagicMock(get_card=lambda _: card))
    agent = ESP32Agent(state, llm=MagicMock())
    agent._autonomy_mode = "reactive"
    return agent, state


def test_reactive_waits_for_edge_result_and_checks_fsm(monkeypatch):
    agent, state = _agent(monkeypatch)
    calls = []
    monkeypatch.setattr("cloud.esp32.tools.set_led_state", lambda **kw: calls.append(kw) or "auto_one")
    assert agent._dispatch_tool("set_led_state", {"state": "ON"}, "esp32_desk_led")
    assert calls == [{"unit_id": "esp32_desk_led", "state": "ON"}]
    state.wait_task_result.assert_called_once_with("auto_one", timeout=20.0)
    state.wait_task_result.return_value = {"result": "timeout"}
    agent._cooldown.clear()
    assert not agent._dispatch_tool("set_led_state", {"state": "ON"}, "esp32_desk_led")
    agent._cooldown.clear()
    assert not agent._dispatch_tool("set_led_state", {"state": "OFF"}, "esp32_desk_led")
    assert len(calls) == 2


def test_reactive_user_hold_and_offline_prevent_publish(monkeypatch):
    agent, _ = _agent(monkeypatch, online=False)
    calls = []
    monkeypatch.setattr("cloud.esp32.tools.set_led_state", lambda **kw: calls.append(kw) or "auto_one")
    assert not agent._dispatch_tool("set_led_state", {"state": "ON"}, "esp32_desk_led")
    agent._user_hold_until = float("inf")
    assert not agent._dispatch_tool("set_led_state", {"state": "ON"}, "esp32_desk_led")
    assert calls == []


def test_lamp_only_declares_supported_tools():
    assert set(TOOL_DECLARATIONS) == {
        "set_led_state", "set_led_color", "blink_led", "list_peers",
        "get_space_context", "request_peer_skill", "send_message",
    }


def test_taught_file_persistence(tmp_path):
    path = tmp_path / "taught.json"
    rule = taught.add("天黑", "调亮", path=path)
    assert taught.list_all(path=path)[0]["id"] == rule["id"]
