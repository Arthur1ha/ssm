"""The shared Graph must observe each result before choosing another tool."""

import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph


@tool
def read_state() -> str:
    """Read the device state."""
    return "state"


@tool
def move() -> str:
    """Move the device once."""
    return "moved"


@tool
def ask_user(question: str) -> str:
    """Ask the user a clarifying question."""
    return question


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        assert tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return next(self.replies)


def call(name, call_id, args=None):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id}])


def run(identifier, saver):
    return GraphRun(identifier, "session", "先看状态再移动", time.time() + 30,
                    agent_id="go2", trace_id="trace")


class MoveArgs(BaseModel):
    """Move for a bounded duration."""
    model_config = ConfigDict(extra="forbid")
    duration: float = Field(gt=0, le=10)


@pytest.mark.asyncio
async def test_graph_observes_result_before_next_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    model = ScriptedModel([call("read_state", "c1"), call("move", "c2"), AIMessage(content="已完成")])
    observed = []

    async def execute(name, params, step_id):
        observed.append((name, step_id, len(model.inputs)))
        return {"result": "ok", "fact": name}

    saver = FileCheckpointSaver(tmp_path)
    result = await run_role_graph(
        run("sequential", FileCheckpointSaver(tmp_path)), model=model,
        tool_schemas={"read_state": read_state, "move": move},
        build_messages=lambda current: [HumanMessage(content=str(current.steps))],
        execute=execute, checkpointer=saver,
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
    )
    assert result.status == "ok"
    assert observed == [("read_state", "c1", 1), ("move", "c2", 2)]
    assert "'fact': 'read_state'" in model.inputs[1][0].content
    assert isinstance(model.inputs[1][-1], ToolMessage)
    assert model.inputs[1][-1].tool_call_id == "c1"
    checkpoint = saver.get_tuple({"configurable": {"thread_id": thread_id_for("go2", "session")}})
    assert checkpoint.checkpoint["channel_values"]["run"]["run_id"] == "sequential"


@pytest.mark.asyncio
async def test_graph_rejects_two_tool_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "move", "args": {}, "id": "c1"},
        {"name": "move", "args": {}, "id": "c2"},
    ])])
    executed = []

    async def execute(name, params, step_id):
        executed.append(name)
        return {"result": "ok"}

    result = await run_role_graph(
        run("multiple", FileCheckpointSaver(tmp_path)), model=model,
        tool_schemas={"move": move}, build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=execute, checkpointer=FileCheckpointSaver(tmp_path),
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
    )
    assert result.status == "blocked"
    assert executed == []


@pytest.mark.asyncio
async def test_pydantic_tool_arguments_rejected_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    model = ScriptedModel([call("move", "bad-args", {"duration": -1})])
    executed = []
    result = await run_role_graph(
        run("invalid", FileCheckpointSaver(tmp_path)), model=model,
        tool_schemas={"move": MoveArgs},
        build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=lambda *_args: executed.append(1) or {"result": "ok"},
        checkpointer=FileCheckpointSaver(tmp_path),
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
    )
    assert result.status == "rejected"
    assert result.steps == []
    assert executed == []


@pytest.mark.asyncio
async def test_uncertain_result_stops_following_tool(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    model = ScriptedModel([call("move", "c1"), call("move", "c2")])
    executed = []

    async def execute(name, params, step_id):
        executed.append(step_id)
        return {"result": "timeout"}

    result = await run_role_graph(
        run("uncertain", FileCheckpointSaver(tmp_path)), model=model,
        tool_schemas={"move": move}, build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=execute, checkpointer=FileCheckpointSaver(tmp_path),
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
    )
    assert result.status == "timeout"
    assert executed == ["c1"]
    assert len(model.inputs) == 1


@pytest.mark.asyncio
async def test_ask_user_resumes_same_thread(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    saver = FileCheckpointSaver(tmp_path)
    model = ScriptedModel([call("ask_user", "c1", {"question": "去哪里？"}),
                           AIMessage(content="明白了")])
    current = run("clarify", saver)
    kwargs = dict(model=model, tool_schemas={"ask_user": ask_user},
                  build_messages=lambda state: [HumanMessage(content=state.supplement or state.goal)],
                  execute=lambda *_args: {"result": "ok"}, checkpointer=saver,
                  effect_ledger=FileEffectLedger(tmp_path / "effects"))
    interrupted = await run_role_graph(current, **kwargs)
    assert interrupted.status == "needs_clarification"
    assert interrupted.question == "去哪里？"
    resumed = await run_role_graph(current, resume=True, resume_value="客厅", **kwargs)
    assert resumed.status == "ok"
    assert model.inputs[-1][0].content == "客厅"


@pytest.mark.asyncio
async def test_reserved_effect_is_never_dispatched_again(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    ledger = FileEffectLedger(tmp_path / "effects")
    thread_id = thread_id_for("go2", "session")
    ledger.reserve("go2", thread_id, "replay", "c1", "move", {})
    model = ScriptedModel([call("move", "c1")])
    executed = []

    async def execute(name, params, step_id):
        executed.append(step_id)
        return {"result": "ok"}

    result = await run_role_graph(
        run("replay", FileCheckpointSaver(tmp_path)), model=model,
        tool_schemas={"move": move}, build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=execute, checkpointer=FileCheckpointSaver(tmp_path), effect_ledger=ledger,
    )
    assert result.status == "unknown"
    assert executed == []


@pytest.mark.asyncio
async def test_observed_effect_reuses_receipt_without_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    ledger = FileEffectLedger(tmp_path / "effects")
    thread_id = thread_id_for("go2", "session")
    ledger.reserve("go2", thread_id, "observed", "c1", "move", {})
    ledger.mark_result("go2", thread_id, "observed", "c1", {"result": "ok", "fact": "done"})
    model = ScriptedModel([call("move", "c1"), AIMessage(content="完成")])
    executed = []

    async def execute(name, params, step_id):
        executed.append(step_id)
        return {"result": "ok"}

    result = await run_role_graph(
        run("observed", FileCheckpointSaver(tmp_path)), model=model,
        tool_schemas={"move": move}, build_messages=lambda current: [HumanMessage(content=str(current.steps))],
        execute=execute, checkpointer=FileCheckpointSaver(tmp_path), effect_ledger=ledger,
    )
    assert result.status == "ok"
    assert executed == []
    assert result.steps[0]["result"]["fact"] == "done"


@pytest.mark.asyncio
async def test_final_reply_is_saved_in_graph_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    saver = FileCheckpointSaver(tmp_path)
    model = ScriptedModel([AIMessage(content="决策草稿")])

    def finalize(current):
        current.response = "管家最终回复"
        current.reply_event_id = "evt_final"

    result = await run_role_graph(
        run("final", saver), model=model, tool_schemas={"read_state": read_state},
        build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=lambda *_args: {"result": "ok"}, checkpointer=saver,
        effect_ledger=FileEffectLedger(tmp_path / "effects"), finalize=finalize,
    )
    checkpoint = saver.get_tuple({"configurable": {"thread_id": thread_id_for("go2", "session")}})
    saved = checkpoint.checkpoint["channel_values"]["run"]
    assert result.response == saved["response"] == "管家最终回复"
    assert saved["reply_event_id"] == "evt_final"
