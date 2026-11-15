"""Multiple native calls execute serially through the existing tool boundary."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph


class SkillCall(BaseModel):
    """Invoke one device's declared skill."""

    unit_id: str
    skill_id: str
    params: dict


def calls():
    # The same pair that the steward returned for the reported user request.
    return [
        {"name": "invoke_skill", "id": "wave", "args": {
            "unit_id": "go2", "skill_id": "go2_sport", "params": {"cmd": "Hello"}}},
        {"name": "invoke_skill", "id": "light", "args": {
            "unit_id": "esp32_desk_led", "skill_id": "adjust_lighting",
            "params": {"goal": "暖白色"}}},
    ]


class Model(JsonReplyModel):
    def __init__(self, batch=None, *, final_only=False):
        self.inputs = []
        self.batch = calls() if batch is None else batch
        self.final_only = final_only

    def bind_tools(self, _tools, **kwargs):
        assert kwargs == {"parallel_tool_calls": False}
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        if len(self.inputs) == 1 and not self.final_only:
            return AIMessage(content="", tool_calls=self.batch)
        assert len(self.inputs) <= 2, "The graph must not need a model turn per queued call"
        return AIMessage(content='{"kind":"final","text":"已核对本轮结果。"}')


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *a, **kw: None)
    return {
        "tool_schemas": {"invoke_skill": SkillCall},
        "build_messages": lambda run: [HumanMessage(content=run.goal)],
        "checkpointer": FileCheckpointSaver(tmp_path / "checkpoints"),
        "effect_ledger": FileEffectLedger(tmp_path / "effects"),
    }


def make_run(agent_id="cloud_orchestrator", run_id="sequential"):
    return GraphRun(run_id, "session", "让机器狗挥挥手，再把灯调成暖白色",
                    time.time() + 60, agent_id=agent_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["cloud_orchestrator", "esp32_desk_led", "go2"])
async def test_multiple_calls_wait_for_receipt_and_checkpoint_before_next(agent_id, runtime):
    run, model = make_run(agent_id), Model()
    executed = []
    busy = False

    async def execute(_name, params, step_id):
        nonlocal busy
        assert not busy
        busy = True
        if step_id == "light":
            snapshot = runtime["checkpointer"].get_tuple({"configurable": {
                "thread_id": thread_id_for(agent_id, run.session_id)}})
            values = snapshot.checkpoint["channel_values"]
            assert values["run"]["steps"][0]["result"] == {"result": "ok", "receipt": "wave", "final": False}
            assert [call["id"] for call in values["pending_calls"]] == ["light"]
        executed.append(("started", step_id, params["unit_id"]))
        await asyncio.sleep(0)
        executed.append(("finished", step_id, params["unit_id"]))
        busy = False
        return {"result": "ok", "receipt": step_id}

    await run_role_graph(run, model=model, execute=execute, **runtime)
    assert [(phase, step_id) for phase, step_id, _unit in executed] == [
        ("started", "wave"), ("finished", "wave"),
        ("started", "light"), ("finished", "light")]
    assert run.status == "ok" and len(run.steps) == 2
    assert len(model.inputs) == 1
    assert [step["result"] for step in run.steps] == [
        {"result": "ok", "receipt": "wave", "final": False},
        {"result": "ok", "receipt": "light", "final": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["executing", "timeout", "unknown",
                                    "cancelled", "error", "blocked", "rejected", "partial"])
async def test_non_success_drops_remaining_calls(status, runtime):
    run, model = make_run(), Model()
    executed = []

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        return {"result": status}

    await run_role_graph(run, model=model, execute=execute, **runtime)
    assert executed == ["wave"] and len(run.steps) == 1
    if status in {"executing", "timeout", "unknown", "cancelled"}:
        assert len(model.inputs) == 1 and run.status == status
    else:
        assert len(model.inputs) == 2 and run.status == "partial"
        receipts = [json.loads(m.content) for m in model.inputs[-1] if isinstance(m, ToolMessage)]
        assert receipts == [{"result": status, "final": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["unknown_tool", "invalid_params"])
async def test_each_queued_call_is_validated_before_dispatch(invalid, runtime):
    batch = calls()
    if invalid == "unknown_tool":
        batch[1]["name"] = "not_allowed"
    else:
        batch[1]["args"].pop("skill_id")
    run, executed = make_run(), []

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        return {"result": "ok"}

    await run_role_graph(run, model=Model(batch), execute=execute, **runtime)
    assert executed == ["wave"] and run.status == "rejected"
    assert run.steps[0]["result"] == {"result": "ok", "final": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["effects", "deadline"])
async def test_limits_are_checked_between_calls(limit, runtime, monkeypatch):
    run, executed = make_run(), []
    clock = [time.time()]
    monkeypatch.setattr("cloud.agents.common.runner.time", SimpleNamespace(
        time=lambda: clock[0], monotonic=time.monotonic))

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        if limit == "deadline":
            clock[0] = run.deadline + 1
        return {"result": "ok"}

    await run_role_graph(run, model=Model(), execute=execute,
                         max_effects=1 if limit == "effects" else 6, **runtime)
    assert executed == ["wave"]
    assert run.status == ("blocked" if limit == "effects" else "timeout")


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [True, False])
async def test_existing_effect_is_reused_or_left_unknown_without_redispatch(observed, runtime):
    run, executed = make_run(), []
    identity = (run.agent_id, thread_id_for(run.agent_id, run.session_id), run.run_id, "wave")
    runtime["effect_ledger"].reserve(*identity, "invoke_skill", calls()[0]["args"])
    if observed:
        runtime["effect_ledger"].mark_result(*identity, {"result": "ok", "receipt": "saved"})

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        return {"result": "ok"}

    await run_role_graph(run, model=Model(), execute=execute, **runtime)
    assert executed == (["light"] if observed else [])
    assert run.status == ("ok" if observed else "unknown")


@pytest.mark.asyncio
async def test_terminal_tool_discards_remaining_calls(runtime):
    run, executed = make_run(), []

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        return {"result": "ok"}

    await run_role_graph(run, model=Model(), execute=execute,
                         terminal_tools=frozenset({"invoke_skill"}), **runtime)
    assert executed == ["wave"] and run.status == "ok"


@pytest.mark.asyncio
async def test_initial_tool_still_uses_the_same_executor(runtime):
    run, executed = make_run(), []
    model = Model(final_only=True)

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        return {"result": "ok"}

    await run_role_graph(run, model=model, execute=execute,
                         initial_tool=("invoke_skill", calls()[0]["args"]), **runtime)
    assert executed == [f"{run.run_id}:initial"] and run.status == "ok"


@pytest.mark.asyncio
async def test_completed_batch_survives_human_input_resume_without_replay(runtime):
    class QuestionModel(Model):
        async def ainvoke(self, messages):
            if not self.inputs:
                return await super().ainvoke(messages)
            self.inputs.append(messages)
            return AIMessage(content='{"kind":"question","text":"接下来去哪里？"}')

    run, executed = make_run(), []

    async def execute(_name, _params, step_id):
        executed.append(step_id)
        return {"result": "ok", "receipt": step_id}

    batch = calls()
    batch[-1]["args"]["final"] = False  # This goal still needs a destination.
    await run_role_graph(run, model=QuestionModel(batch), execute=execute, **runtime)
    assert run.status == "needs_clarification" and executed == ["wave", "light"]
    resumed_runtime = {**runtime,
        "checkpointer": FileCheckpointSaver(runtime["checkpointer"].root),
        "effect_ledger": FileEffectLedger(runtime["effect_ledger"].root)}
    reopened = make_run()
    await run_role_graph(reopened, model=Model(final_only=True), execute=execute,
                         resume=True, resume_value="客厅", **resumed_runtime)
    assert reopened.status == "ok" and len(reopened.steps) == 2
    assert executed == ["wave", "light"]
