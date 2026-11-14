"""Before/after facts stay distinct across tool turns and checkpoints."""

import json
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.lamp import prompts
from cloud.agents.lamp.tools.schemas import SetLedColor


UNIT = "esp32_desk_led"


@pytest.fixture
def facts(monkeypatch):
    physical = {"ism": "OFF", "brightness": 0, "r": 0, "g": 0, "b": 0,
                "ts": time.time()}
    space = {"peers": [], "activity": None, "environment": []}
    agent = SimpleNamespace(_state=SimpleNamespace(
        actuator_snapshot=lambda: {UNIT: {"state": deepcopy(physical)}}))
    monkeypatch.setattr(prompts, "get_profile", lambda: {
        "agent_id": UNIT, "name": "灯", "prompt": "自然说话", "traits": {}})
    monkeypatch.setattr(prompts, "agent_names", lambda: {})
    monkeypatch.setattr(prompts, "decision_context", lambda *_a, **_k: {})
    monkeypatch.setattr(prompts, "get_space_context", lambda *_a, **_k: space)
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    return agent, physical, space


def context(messages):
    return json.loads(next(m.content.split("\n", 1)[1]
                          for m in messages if m.name == "context"))


@pytest.mark.asyncio
async def test_off_baseline_survives_successful_color_change_and_checkpoint(facts, tmp_path):
    agent, physical, _ = facts
    seen = []

    class Model(JsonReplyModel):
        def bind_tools(self, *_a, **_k):
            return self

        async def ainvoke(self, messages):
            seen.append(context(messages))
            if len(seen) == 1:
                return AIMessage(content="", tool_calls=[{
                    "name": "set_led_color", "id": "warm-white",
                    "args": {"r": 255, "g": 190, "b": 125, "brightness": 180}}])
            return AIMessage(content="暖白亮起来了。")

    async def execute(_name, params, _step):
        physical.update(params, ism="ON", ts=time.time())
        return {"result": "ok", "task_id": "physical-warm"}

    run = GraphRun("before-after", "session", "调成暖白色", time.time() + 30,
                   agent_id=UNIT, origin_kind="user_delegated")
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    await run_role_graph(run, model=Model(), tool_schemas={"set_led_color": SetLedColor},
        build_messages=lambda current: prompts.decision_messages(current, agent=agent, unit_id=UNIT),
        execute=execute, checkpointer=saver, effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert seen[0]["lamp"][UNIT]["state"]["ism"] == "OFF"
    assert seen[1]["lamp"][UNIT]["state"]["ism"] == "ON"
    assert seen[1]["lamp_before"] == seen[0]["lamp_before"]
    assert seen[1]["lamp_before"]["values"]["brightness"] == 0
    saved = saver.get_tuple({"configurable": {"thread_id": thread_id_for(UNIT, "session")}})
    restored = GraphRun(**saved.checkpoint["channel_values"]["run"])
    assert restored.initial_context["lamp_before"] == seen[0]["lamp_before"]
    assert restored.status == "ok"


def test_stale_baseline_and_current_cache_cannot_claim_a_color(facts):
    agent, physical, _ = facts
    physical.update(ism="ON", r=255, g=190, b=125, brightness=180, ts=time.time() - 1000)
    run = GraphRun("stale", "session", "暖白", time.time() + 30)
    data = context(prompts.decision_messages(run, agent=agent, unit_id=UNIT))
    assert data["lamp_before"]["status"] == "stale"
    assert "values" not in data["lamp_before"]
    assert data["lamp"][UNIT]["status"] == "stale"
    assert data["lamp"][UNIT]["state"] == {}


def test_finite_blink_count_is_preserved_in_lighting_observation(facts):
    agent, physical, _ = facts
    physical.update(ism="BLINKING", count=3)
    run = GraphRun("blinking", "session", "记住现在的闪烁", time.time() + 30)
    data = context(prompts.decision_messages(run, agent=agent, unit_id=UNIT))
    assert data["lamp_before"]["values"]["count"] == 3
    assert data["lamp"][UNIT]["state"]["count"] == 3


def test_legacy_run_with_executed_steps_has_no_invented_baseline(facts):
    agent, physical, _ = facts
    physical.update(ism="ON", brightness=180)
    run = GraphRun("legacy", "session", "暖白", time.time() + 30,
                   steps=[{"tool": "set_led_color", "result": {"result": "ok"}}])
    data = context(prompts.decision_messages(run, agent=agent, unit_id=UNIT))
    assert data["lamp_before"]["status"] == "unknown"
    assert "values" not in data["lamp_before"]
    assert data["lamp"][UNIT]["state"]["ism"] == "ON"


def test_model_turn_keeps_its_own_server_observation(facts):
    agent, _, space = facts
    space["environment"] = [{"source": "light", "values": {"value": 0}}]
    run = GraphRun("evidence", "session", "暖白", time.time() + 30)
    prompts.decision_messages(run, agent=agent, unit_id=UNIT)
    space["environment"][0]["values"]["value"] = 818
    assert run.decision_space["environment"][0]["values"]["value"] == 0


def test_delegated_lighting_keeps_game_instructions_out_of_simple_commands(facts, monkeypatch):
    agent, _, space = facts
    space["peers"] = [{"unit_id": "go2"}]
    monkeypatch.setattr("cloud.peers.games.playable_games", lambda *_a: pytest.fail("no game lookup"))
    run = GraphRun("lighting", "session", "暖白", time.time() + 30)
    messages = prompts.decision_messages(run, agent=agent, unit_id=UNIT, lighting_only=True)
    assert context(messages)["playable_games"] == []
    assert "evidence_refs" not in messages[0].content
    assert "执行前" in messages[0].content


def test_delegated_lamp_entry_dispatches_once_and_reuses_completed_run(facts, monkeypatch, tmp_path):
    from cloud.agents.lamp import graph
    from cloud.esp32.capability_chain_checks import card
    from cloud.space.execution import ExecutionContext

    agent, physical, _ = facts
    for variable in ("SSM_CHECKPOINT_DIR", "SSM_EFFECT_LEDGER_DIR", "SSM_RUN_INDEX_DIR"):
        monkeypatch.setenv(variable, str(tmp_path / variable))
    monkeypatch.setattr(graph.cards, "get_registry", lambda: SimpleNamespace(get_card=lambda _uid: card()))
    monkeypatch.setattr(graph, "get_profile", prompts.get_profile)
    monkeypatch.setattr(graph, "submit_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(graph, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.api.intents.publish_agent_event", lambda *_a, **_k: {})
    dispatched = []

    def physical_action(**kwargs):
        dispatched.append(kwargs)
        physical.update(kwargs["params"], ism="ON", ts=time.time())
        return {"result": "ok", "task_id": kwargs["task_id"]}

    monkeypatch.setattr("cloud.agents.lamp.tools.execute.execute_physical", physical_action)

    class Model(JsonReplyModel):
        calls = 0

        def bind_tools(self, tools, **_kwargs):
            color = next(item for item in tools if item["function"]["name"] == "set_led_color")
            assert "evidence_refs" not in color["function"]["parameters"]["properties"]
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            data = context(messages)
            assert data["lamp_before"]["values"]["ism"] == "OFF"
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[{"name": "set_led_color", "id": "warm",
                    "args": {"r": 255, "g": 190, "b": 125, "brightness": 180}}])
            assert data["lamp"][UNIT]["state"]["ism"] == "ON"
            return AIMessage(content="暖白亮起来了。")

    model = Model()
    agent._llm = model
    agent.mark_user_command = lambda _uid: None
    trusted = ExecutionContext("session", "admin", False, "main", "user_delegated", time.time() + 60)
    arguments = dict(lighting_only=True, run_id="warm-entry", parent_run_id="steward")
    result = graph.run_lamp_loop(agent, "调成暖白色", UNIT, trusted, lambda *_a, **_k: None, **arguments)
    assert result["result"] == "ok" and len(dispatched) == 1 and model.calls == 2
    replay = graph.run_lamp_loop(agent, "调成暖白色", UNIT, trusted, lambda *_a, **_k: None, **arguments)
    assert replay["result"] == "ok" and replay["reply"] == result["reply"]
    assert len(dispatched) == 1 and model.calls == 2
