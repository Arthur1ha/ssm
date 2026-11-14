"""Ordinary commands and grounded game decisions have distinct tool contracts."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from cloud.agents.lamp.tools.execute import execute_lamp_tool
from cloud.agents.lamp.tools.schemas import visible_schemas
from cloud.space.decisions import decision_evidence


@pytest.mark.parametrize("lighting_only", [True, False])
def test_user_command_schema_excludes_observation_metadata(lighting_only):
    schema = visible_schemas(lighting_only=lighting_only, user_delegated=True)["set_led_color"]
    assert schema(r=255, g=190, b=125, brightness=180).r == 255
    with pytest.raises(ValidationError):
        schema(r=255, g=190, b=125, decision_reason="用户要暖白", evidence_refs=["sensor"])


def test_game_schema_requires_reason_and_observation_refs():
    schema = visible_schemas(lighting_only=True, game_setup=True)["set_led_color"]
    with pytest.raises(ValidationError):
        schema(r=255, g=190, b=125)
    with pytest.raises(ValidationError):
        schema(r=255, g=190, b=125, decision_reason="选择暖白", evidence_refs=[])
    assert schema(r=255, g=190, b=125, decision_reason="刚才看见白色玩偶",
                  evidence_refs=["evt_seen"]).evidence_refs == ["evt_seen"]


def test_user_color_command_does_not_read_changing_sensor_evidence(monkeypatch):
    get_space = Mock(side_effect=AssertionError("ordinary command must not depend on sensor refs"))
    monkeypatch.setattr("cloud.space.context.get_space_context", get_space)
    monkeypatch.setattr("cloud.cards.registry.get_registry", lambda: SimpleNamespace(get_card=lambda _: {"unit_id": "lamp"}))
    physical = Mock(return_value={"result": "ok", "task_id": "physical"})
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    context = SimpleNamespace(session_id="chat_user", origin_kind="user_delegated")
    result = execute_lamp_tool("set_led_color", {"r": 255, "g": 190, "b": 125, "brightness": 180},
        "step", agent=SimpleNamespace(_state=None), unit_id="lamp", context=context,
        dispatch=Mock(), lighting_only=True, run_id="run")
    assert result["result"] == "ok"
    physical.assert_called_once()
    get_space.assert_not_called()


def test_game_setup_uses_saved_observation_but_rechecks_live_user_hold(monkeypatch):
    monkeypatch.setattr("cloud.space.decisions.time.time", lambda: 1000)
    observed = {"activity": {"activity_id": "act", "status": "active", "revision": 1},
        "peers": [], "constraints": [],
        "environment": [{"source": "light", "status": "current", "as_of": 999,
                         "values": {"value": 0, "level": "DARK"}}],
        "recent_discoveries": [{"event_id": "evt_seen", "activity_id": "act",
            "source_unit_id": "go2", "event_type": "observation", "ts": 999,
            "summary": "地面有一个白色玩偶"}]}
    ref = next(item["ref"] for item in decision_evidence(observed) if item["kind"] == "environment")
    current = deepcopy(observed)
    current["environment"][0]["values"]["value"] = 818
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: current)
    monkeypatch.setattr("cloud.cards.registry.get_registry", lambda: SimpleNamespace(get_card=lambda _: {"unit_id": "lamp"}))
    record_event = Mock(return_value={"event_id": "action"})
    monkeypatch.setattr("cloud.space.activity.get_activity_store", lambda: SimpleNamespace(
        current=lambda: current["activity"], record_event=record_event))
    monkeypatch.setattr("cloud.peers.games.activity_allows", lambda *_: True)
    physical = Mock(return_value={"result": "ok"})
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    context = SimpleNamespace(session_id="peer_game_color_test", origin_kind="agent_autonomous")
    agent = SimpleNamespace(_state=None, _should_act=lambda: False, _device_online=lambda: True)
    args = {"r": 255, "g": 255, "b": 255, "decision_reason": "刚才看见白色玩偶",
            "evidence_refs": [ref, "evt_seen"]}
    def execute():
        return execute_lamp_tool("set_led_color", args, "step", agent=agent, unit_id="lamp",
            context=context, dispatch=Mock(), lighting_only=True, run_id="run",
            decision_space=observed)
    assert execute()["error"] == "game_activity_unavailable"
    physical.assert_not_called()
    record_event.assert_not_called()
    agent._should_act = lambda: True
    result = execute()
    assert result["result"] == "ok"
    assert result["decision_basis"]["evidence"][0]["ref"] == ref
    assert "读数：0" in result["decision_basis"]["evidence"][0]["summary"]
    assert physical.call_args.kwargs["params"] == {"r": 255, "g": 255, "b": 255}


def test_invalid_game_evidence_is_an_explicit_predispatch_refusal(monkeypatch):
    monkeypatch.setattr("cloud.space.context.get_space_context", lambda *_a, **_k: {})
    physical = Mock()
    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical)
    result = execute_lamp_tool("set_led_color", {"r": 255, "g": 255, "b": 255},
        "step", agent=SimpleNamespace(_state=None), unit_id="lamp",
        context=SimpleNamespace(session_id="peer_game_color_test", origin_kind="agent_autonomous"),
        dispatch=Mock(), lighting_only=True, run_id="run")
    assert result == {"result": "rejected", "error": "decision_reason_required",
                      "execution_phase": "preflight"}
    physical.assert_not_called()
