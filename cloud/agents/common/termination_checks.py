"""All three roles obey the same explicit action/termination contract."""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, ConfigDict

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.replies import finalize_reply
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.tool_results import task_result


class Action(BaseModel):
    """Execute a device action with strict business arguments."""

    model_config = ConfigDict(extra="forbid")
    value: int


class Model:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.inputs = []

    def bind_tools(self, tools, **_kwargs):
        self.tools = tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return next(self.responses)  # A planning call after final fails the test.


def call(step="one", **args):
    return {"name": "action", "id": step, "args": {"value": 1, **args}}


def decision(*calls):
    return AIMessage(content="", tool_calls=list(calls))


@pytest.fixture(params=["cloud_orchestrator", "esp32_desk_led", "go2"])
def runtime(request, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)
    return GraphRun("ending", "session", "完成当前目标", time.time() + 60,
                    agent_id=request.param), {
        "tool_schemas": {"action": Action},
        "build_messages": lambda run: [HumanMessage(content=run.goal)],
        "checkpointer": FileCheckpointSaver(tmp_path / "checkpoints"),
        "effect_ledger": FileEffectLedger(tmp_path / "effects"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "accepted"])
async def test_single_action_ends_without_more_planning_and_replay_does_not_dispatch(runtime, status):
    run, options = runtime
    executed = []

    def execute(name, params, step_id):
        executed.append((name, params, step_id))
        return {"result": status, "task_id": "device-task"}

    for _ in range(2):
        model = Model(decision(call()))
        await run_role_graph(run, model=model, execute=execute, **options)
        assert run.status == "ok" and len(model.inputs) == 1
        assert run.steps[-1]["result"]["final"] is True
        assert run.steps[-1]["result"]["result"] == "ok"
    assert executed == [("action", {"value": 1}, "one")]
    saved = options["checkpointer"].get_tuple({"configurable": {
        "thread_id": thread_id_for(run.agent_id, run.session_id)}})
    assert saved.checkpoint["channel_values"]["pending_calls"] == []
    assert saved.checkpoint["channel_values"]["run"]["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["call", "executor"])
@pytest.mark.parametrize("status", ["ok", "accepted"])
async def test_explicit_final_discards_even_queued_calls(runtime, source, status):
    run, options = runtime
    model = Model(decision(call(final=source == "call"), call("unwanted")))
    executed = []

    def execute(_name, params, step_id):
        assert params == {"value": 1}  # Control metadata never reaches hardware.
        executed.append(step_id)
        return {"result": status, **({"final": True} if source == "executor" else {})}

    await run_role_graph(run, model=model, execute=execute, **options)
    assert run.status == "ok" and executed == ["one"] and len(model.inputs) == 1


@pytest.mark.asyncio
async def test_explicit_continue_allows_dependent_next_step_but_no_extra_round(runtime):
    run, options = runtime
    model = Model(decision(call(final=False)), decision(call("two")))
    executed = []

    def execute(_name, params, step_id):
        executed.append(step_id)
        return {"result": "accepted", "observed": step_id}

    await run_role_graph(run, model=model, execute=execute, **options)
    assert run.status == "ok" and executed == ["one", "two"] and len(model.inputs) == 2
    receipt = next(m for m in model.inputs[-1] if isinstance(m, ToolMessage))
    assert json.loads(receipt.content) == {
        "result": "ok", "device_result": "accepted", "observed": "one", "final": False}


@pytest.mark.asyncio
async def test_queries_can_continue_and_final_query_can_end(runtime):
    run, options = runtime
    model = Model(decision(call()), decision(call("two", final=True)))
    await run_role_graph(run, model=model, execute=lambda *_: {"result": "ok"},
                         effectful=lambda _name: False, **options)
    assert run.status == "ok" and len(model.inputs) == 2
    assert [step["result"]["final"] for step in run.steps] == [False, True]


@pytest.mark.asyncio
async def test_executor_final_failure_ends_without_replanning(runtime):
    run, options = runtime
    model = Model(decision(call(), call("unwanted")))
    await run_role_graph(run, model=model,
        execute=lambda *_: {"result": "blocked", "final": True, "error": "unavailable"}, **options)
    assert run.status == "blocked" and len(run.steps) == 1 and len(model.inputs) == 1


@pytest.mark.asyncio
async def test_final_success_does_not_erase_previous_unresolved_failure(runtime):
    run, options = runtime
    model = Model(decision(call()), decision(call("two")))
    await run_role_graph(run, model=model,
        execute=lambda _name, _params, step: {"result": "error" if step == "one" else "ok"}, **options)
    assert run.status == "partial" and len(model.inputs) == 2


@pytest.mark.asyncio
async def test_final_reply_can_speak_once_without_reopening_tools(runtime):
    run, options = runtime
    planner = Model(decision(call()))
    speaker = Model(AIMessage(content="已经调好了。"))
    await run_role_graph(run, model=planner, execute=lambda *_: {"result": "accepted"},
        finalize=lambda current: finalize_reply(current, model=speaker, instructions="直接回应"), **options)
    assert run.status == "ok" and run.response == "已经调好了。"
    assert len(planner.inputs) == len(speaker.inputs) == 1
    assert not hasattr(speaker, "tools")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["true", 1, None])
async def test_invalid_call_final_never_dispatches(runtime, invalid):
    run, options = runtime
    model = Model(decision(call(final=invalid)))
    await run_role_graph(run, model=model,
        execute=lambda *_: pytest.fail("invalid metadata dispatched an action"), **options)
    assert run.status == "rejected" and run.steps == []


@pytest.mark.parametrize("receipt", [None, {}, {"result": []}, {"result": "ok", "final": "true"}])
def test_invalid_result_ends_as_error(receipt):
    assert task_result(receipt, final=False) == {
        "result": "error", "error": "invalid_tool_result", "final": True}


def test_acceptance_does_not_retain_an_unconfirmed_error_in_success_projection():
    receipt = {"result": "accepted", "error": "command_result_unconfirmed", "task_id": "original"}
    assert task_result(receipt, final=True) == {
        "result": "ok", "device_result": "accepted", "task_id": "original", "final": True}
    assert receipt["error"] == "command_result_unconfirmed"  # Original receipt stays intact.
