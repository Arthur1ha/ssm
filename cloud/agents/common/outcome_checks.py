"""Recovery changes outcome projection, never dispatch or original receipts."""

import copy
import json
import time

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.messages import role_messages
from cloud.agents.common.outcomes import completed_status, effective_steps, outcome_summary
from cloud.agents.common.replies import factual_fallback, reply_facts
from cloud.agents.common.runner import GraphRun, run_role_graph


def lamp_step(step_id, result, *, params=None, tool="set_led_color", preflight=False):
    return {"step_id": step_id, "tool_call_id": step_id, "tool": tool,
            "params": params if params is not None else {"r": 255, "g": 190, "b": 125, "brightness": 180},
            "effectful": True, "dispatch_status": "observed",
            "result": {"result": result, **({"execution_phase": "preflight"} if preflight else {})}}


def test_same_lamp_request_can_recover_preflight_refusals_without_erasing_receipts():
    first = lamp_step("one", "rejected", preflight=True)
    first["params"].update(decision_reason="当前灯是熄的", evidence_refs=["old"])
    second = lamp_step("two", "blocked", preflight=True)
    second["params"].update(decision_reason="仍需暖白", evidence_refs=["new"])
    completed = lamp_step("three", "ok")
    steps = [first, second, completed]
    before = copy.deepcopy(steps)
    assert completed_status(steps) == "ok"
    assert effective_steps(steps) == [completed]
    assert outcome_summary(steps) == {
        "recovered_attempts": [{"step_id": "one", "completed_by": "three"},
                               {"step_id": "two", "completed_by": "three"}],
        "unresolved_attempts": []}
    assert steps == before


@pytest.mark.parametrize("result", ["unknown", "timeout", "accepted", "executing", "cancelled", "error"])
def test_uncertainty_and_physical_failures_are_never_hidden_by_later_success(result):
    assert completed_status([lamp_step("one", result, preflight=True),
                             lamp_step("two", "ok")]) == "partial"


def test_legacy_refusal_without_adapter_preflight_marker_is_conservative():
    assert completed_status([lamp_step("one", "rejected"), lamp_step("two", "ok")]) == "partial"


@pytest.mark.parametrize("intervening", [
    lamp_step("other-color", "ok", params={"r": 0, "g": 0, "b": 255}),
    lamp_step("other-state", "ok", tool="set_led_state", params={"state": "ON"}),
    {"step_id": "peer", "tool": "request_peer_skill", "effectful": True,
     "dispatch_status": "observed", "params": {}, "result": {"result": "ok"}},
])
def test_intervening_effect_prevents_retroactive_ordering_success(intervening):
    steps = [lamp_step("one", "rejected", preflight=True), intervening,
             lamp_step("three", "ok")]
    assert completed_status(steps) == "partial"
    assert outcome_summary(steps)["recovered_attempts"] == []


def test_read_only_observation_does_not_break_retry_but_its_failure_is_preserved():
    query = {"step_id": "query", "tool": "get_space_context", "effectful": False,
             "dispatch_status": "observed", "params": {}, "result": {"result": "error"}}
    steps = [lamp_step("one", "rejected", preflight=True), query, lamp_step("three", "ok")]
    assert effective_steps(steps) == [query, steps[-1]]
    assert completed_status(steps) == "partial"


def test_success_for_other_target_or_parameters_does_not_recover_failed_request():
    for params in ({"r": 255, "g": 190, "b": 125, "brightness": 120},
                   {"r": 255, "g": 190, "b": 125, "brightness": 180, "unit_id": "other"}):
        assert completed_status([lamp_step("one", "rejected", preflight=True),
                                 lamp_step("two", "ok", params=params)]) == "partial"


def test_public_lamp_status_and_factual_reply_agree_after_recovery():
    from cloud.agents.lamp.graph import _public_status

    steps = [lamp_step("one", "rejected", preflight=True), lamp_step("two", "ok")]
    run = GraphRun("recovered", "session", "调成暖白", time.time() + 10,
                   steps=steps, status="partial", agent_id="esp32_desk_led")
    assert _public_status(run) == "ok"
    run.status = completed_status(steps)
    answer = factual_fallback(run.agent_id, run.status, run)
    assert "调好了" in answer and "没做成" not in answer
    run.initial_context = {"lamp_before": {"status": "current", "values": {"ism": "OFF"}}}
    assert reply_facts(run)["initial_context"] == run.initial_context
    assert reply_facts(run)["outcomes"]["unresolved_attempts"] == []
    run.steps = [lamp_step("one", "rejected", preflight=True),
                 lamp_step("two", "ok", tool="set_led_state", params={"state": "OFF"})]
    run.status = "partial"
    assert _public_status(run) == "partial"


def test_older_run_checkpoint_uses_empty_context_defaults():
    run = GraphRun(**{"run_id": "old", "session_id": "session", "goal": "暖白", "deadline": time.time() + 10})
    assert run.initial_context == {} and run.decision_space == {}


@pytest.mark.asyncio
async def test_child_partial_result_is_preserved_and_not_treated_as_completion(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *a, **k: None)
    responses = iter([
        AIMessage(content="", tool_calls=[{"name": "invoke_skill", "args": {}, "id": "child"}]),
        AIMessage(content="部分完成，后一步没有执行。"),
    ])

    class Model:
        def bind_tools(self, *_a, **_k): return self
        def bind(self, **_k): return self
        async def ainvoke(self, _messages): return next(responses)

    receipt = {"result": "partial", "steps": [lamp_step("one", "ok"), lamp_step("two", "rejected")]}
    run = GraphRun("partial", "session", "先暖白再闪烁", time.time() + 30, agent_id="cloud_orchestrator")
    await run_role_graph(run, model=Model(),
        tool_schemas={"invoke_skill": {"name": "invoke_skill", "description": "调用公开技能",
                                     "parameters": {"type": "object", "properties": {}}}},
        build_messages=lambda current: role_messages("按真实结果回复", current.goal),
        execute=lambda *_a: receipt, checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert run.status == "partial" and run.steps[0]["result"] == receipt


@pytest.mark.asyncio
async def test_graph_uses_recovery_with_original_steps_and_checkpointed_context(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *a, **k: None)
    inputs = []
    executions = []
    arguments = {"r": 255, "g": 190, "b": 125, "brightness": 180}
    responses = iter([
        AIMessage(content="", tool_calls=[{"name": "set_led_color", "args": arguments, "id": "one"}]),
        AIMessage(content="", tool_calls=[{"name": "set_led_color", "args": arguments, "id": "two"}]),
        AIMessage(content="调好了，暖白。"),
    ])

    class Model:
        def bind_tools(self, *_a, **_k): return self
        def bind(self, **_k): return self
        async def ainvoke(self, messages):
            inputs.append(messages)
            return next(responses)

    async def execute(name, params, step_id):
        executions.append(step_id)
        return ({"result": "rejected", "error": "decision_evidence_unavailable", "execution_phase": "preflight"}
                if step_id == "one" else {"result": "ok", "task_id": "physical"})

    run = GraphRun("retry", "session", "暖白", time.time() + 30, agent_id="esp32_desk_led",
                   initial_context={"lamp_before": {"values": {"ism": "OFF"}}})
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    await run_role_graph(run, model=Model(),
        tool_schemas={"set_led_color": {"name": "set_led_color", "description": "设置灯色",
                                      "parameters": {"type": "object", "properties": {}}}},
        build_messages=lambda current: role_messages("按真实结果回复", current.goal),
        execute=execute, checkpointer=saver, effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert run.status == "ok" and executions == ["one", "two"]
    assert [step["result"]["result"] for step in run.steps] == ["rejected", "ok"]
    summaries = [json.loads(message.content.split("\n", 1)[1])["outcomes"]
                 for message in inputs[-1] if message.name == "context" and "recovered_attempts" in message.content]
    assert summaries[0]["recovered_attempts"] == [{"step_id": "one", "completed_by": "two"}]
    from cloud.agents.common.context import thread_id_for
    saved = saver.get_tuple({"configurable": {"thread_id": thread_id_for(run.agent_id, run.session_id)}})
    assert saved.checkpoint["channel_values"]["run"]["initial_context"] == run.initial_context
