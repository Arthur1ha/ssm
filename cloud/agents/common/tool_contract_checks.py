"""Verify that tool usage reaches the model through native declarations."""

import time

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.messages import role_messages
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.training.tools import SCHEMAS as TRAINING_SCHEMAS
from cloud.agents.go2.tools.declarations import tool_schemas
from cloud.agents.lamp.tools.schemas import visible_schemas
from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["go2", "lamp", "steward"])
async def test_native_tools_carry_usage_and_parameters(role, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *a, **k: None)
    declarations = ({**tool_schemas(), **TRAINING_SCHEMAS} if role == "go2" else
                    visible_schemas(lighting_only=False) if role == "lamp" else
                    {**STEWARD_TOOL_SCHEMAS, **TRAINING_SCHEMAS})
    received = []

    class Model:
        def bind_tools(self, tools, **options):
            received.extend(tools)
            assert options == {"parallel_tool_calls": False}
            return self

        def bind(self, **options):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content='{"kind":"final","text":"你好。"}')

    run = GraphRun("tools", "session", "你好", time.time() + 30, agent_id=role)
    await run_role_graph(run, model=Model(), tool_schemas=declarations,
        build_messages=lambda value: role_messages("自然回应", value.goal),
        execute=lambda *a: pytest.fail("conversation must not execute tools"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    tools = {item["function"]["name"]: item["function"] for item in received}
    assert tools.keys() == declarations.keys()
    proposal = tools["propose_training"]
    assert "list_training" in proposal["description"]
    assert "situation" in proposal["description"]
    assert "definition" in proposal["parameters"]["properties"]
    if role == "go2":
        move = tools["go2_move"]
        assert "distance_m" in move["parameters"]["properties"]
        assert "stop.result" in move["description"]
        assert "seen_not_reached" in tools["go2_find_object"]["description"]
        assert "盲走" in tools["go2_navigate_to_visible_target"]["description"]
    elif role == "lamp":
        assert "持续闪烁" in tools["blink_led"]["description"]
        assert "game_invite" in tools["send_message"]["description"]
    else:
        assert "直接用户请求" in tools["start_activity"]["description"]
        assert "连续片段" in tools["set_space_constraint"]["description"]
