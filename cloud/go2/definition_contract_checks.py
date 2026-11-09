"""Change-source acceptance checks: consumers must follow the device declarations."""
from dataclasses import replace

import pytest

from cloud.go2 import actions, state
from cloud.go2.card import _build_go2_card, _go2_state_machine
from cloud.go2.tools.registry import get_card_skill
from cloud.go2.connection.webrtc import Go2Connection
from cloud.go2.tools import registry
from cloud.go2.tools.executor import validate_params
from cloud.agents.go2.tools.declarations import tool_schemas


def test_action_copy_preview_and_deadline_follow_one_definition(monkeypatch):
    monkeypatch.setitem(actions.ACTIONS, "Hello", replace(
        actions.ACTIONS["Hello"], label="测试问候", description="等待本次关节反馈", timeout_s=75,
        preview="test-pose"))
    skill = get_card_skill("go2_sport")
    tool = tool_schemas()["go2_sport"]
    machine = next(region for region in _go2_state_machine()["regions"] if region["id"] == "body")
    assert skill["description"] == tool["description"]
    assert "测试问候(Hello)：等待本次关节反馈" in tool["description"]
    assert skill["params_schema"] == tool["parameters"]
    assert skill["params_schema"]["properties"]["cmd"]["x-enum-labels"]["Hello"] == "测试问候"
    assert machine["views"]["greeting"]["label"] == "测试问候"
    assert machine["views"]["greeting"]["pose"] == "test-pose"
    assert next(edge for edge in machine["transitions"] if edge["trigger"] == "Hello")["label"] == "测试问候"
    assert registry.get_tool_spec("go2_sport").timeout_s == 85
    assert actions.ACTIONS["Hello"].api_id == 1016


def test_axis_limit_reaches_card_model_receiver_and_adapter(monkeypatch):
    monkeypatch.setitem(actions.VELOCITY_LIMITS, "x", .2)
    skill = get_card_skill("go2_move")
    assert skill["params_schema"] == tool_schemas()["go2_move"]["parameters"]
    assert skill["params_schema"]["properties"]["speed"]["maximum"] == .2
    assert skill["params_schema"]["properties"]["speed"]["default"] == .2
    with pytest.raises(ValueError, match="maximum"):
        validate_params(skill["params_schema"], {"direction": "forward", "speed": .21})
    with pytest.raises(ValueError, match="range"):
        Go2Connection._validate_velocity(x=.21)
    widget = next(w for w in _build_go2_card()["widgets"] if w["type"] == "joystick")
    assert widget["params_schema"]["properties"]["x"]["maximum"] == .2


def test_life_guard_change_updates_runtime_and_card(monkeypatch):
    spec = state.LIFE_ACTION_SPECS["start_explore"]
    monkeypatch.setitem(state.LIFE_ACTION_SPECS, spec.action,
                        replace(spec, guards=(*spec.guards, "not_remote")))
    runtime = state.Go2State()
    runtime.connection("online")
    runtime.body("standing", confirmation="observed")
    runtime.commit("remote", control_mode="remote")
    assert runtime.life_reasons()[spec.action] == state.LIFE_GUARDS["not_remote"]
    machine = _go2_state_machine()
    life = next(region for region in machine["regions"] if region["id"] == "life")
    transition = next(edge for edge in life["transitions"] if edge["trigger"] == spec.action)
    assert "非遥控" in transition["trigger_condition"]
    assert transition["required_actor"] == spec.required_actor
    assert get_card_skill("go2_life")["params_schema"] == state.life_tool_schema()


def test_public_binding_and_schema_have_one_owner(monkeypatch):
    monkeypatch.setitem(registry.CARD_SKILLS, "go2_status", {
        **registry.CARD_SKILLS["go2_status"], "tool_name": "go2_observe"})
    entry = get_card_skill("go2_status")
    assert entry["tool_name"] == "go2_observe"
    assert entry["params_schema"] == registry.get_tool_spec("go2_observe").params_schema
    assert entry["description"] == registry.get_tool_spec("go2_observe").description


def test_public_skills_are_resolvable_without_publishing_private_tools():
    for skill_id in registry.CARD_SKILLS:
        skill = get_card_skill(skill_id)
        if skill_id == "go2_chat":
            assert skill["tool_name"] is None
            assert skill["invoke"]["action"] == "CHAT"
            assert skill["params_schema"] == registry.CHAT_PARAMS_SCHEMA
        else:
            assert registry.get_tool_spec(skill["tool_name"]) is not None
        assert "api_id" not in str(skill)
    assert get_card_skill("go2_navigate_to_coordinate") is None
    assert get_card_skill("send_message") is None


def test_reconnect_resets_mode_in_the_fact_store():
    runtime = state.Go2State()
    runtime.connection("online")
    runtime.commit("allow_explore", autonomy_mode="free_explore", exploration_enabled=True)
    runtime.connection("offline")
    assert runtime.snapshot()["autonomy_mode"] == "manual"
    assert runtime.snapshot()["exploration_enabled"] is False
    runtime.connection("connecting")
    runtime.connection("online")
    assert runtime.snapshot()["autonomy_mode"] == "manual"


@pytest.mark.asyncio
async def test_relative_move_omitted_speed_uses_the_changed_declaration(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from cloud.go2.tools import tools, executor
    from cloud.go2.navigation.navigator import navigator
    monkeypatch.setitem(actions.VELOCITY_LIMITS, "x", .2)
    monkeypatch.setattr(tools, "go2", SimpleNamespace(is_connected=True))
    move = AsyncMock(return_value={"result": "ok", "distance_m": 1.})
    monkeypatch.setattr(navigator, "move_relative", move)
    result = await tools.go2_move("forward", distance_m=1.)
    assert move.await_args.args == ("forward",)
    assert move.await_args.kwargs['speed'] == .2
    assert move.await_args.kwargs['distance_m'] == 1.
    assert 'duration' not in get_card_skill('go2_move')['params_schema']['properties']
    assert result["result"] == "ok"
