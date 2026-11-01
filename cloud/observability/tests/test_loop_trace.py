"""Graph execution emits one correlated model, tool, checkpoint and reply trace."""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.observability import bind_context, configure_logging


class SetLedState(BaseModel):
    state: str


def _events(directory):
    path = next(directory.glob("session-*.jsonl"))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class Model:
    def __init__(self, messages):
        self.messages = iter(messages)

    def bind_tools(self, _tools, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        return self

    async def ainvoke(self, _messages):
        result = next(self.messages)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_graph_trace_records_durable_dispatch_and_receipt(tmp_path):
    configure_logging("api", log_dir=tmp_path / "logs")
    trace_id = "tr_0123456789abcdef0123456789abcdef"
    run = GraphRun("run_trace", "session", "turn on the light", time.time() + 10,
                   trace_id=trace_id, agent_id="esp32_desk_led")
    recorded = []

    def record(current, step):
        if step is not None:
            recorded.append((step["step_id"], step["dispatch_status"]))

    async def execute(_name, _params, step_id):
        assert recorded[-1] == (step_id, "pending")
        return {"result": "ok", "task_id": "task_one"}

    with bind_context(trace_id=trace_id, run_id=run.run_id,
                      agent_id=run.agent_id, session_id=run.session_id):
        await run_role_graph(
            run, model=Model([
                AIMessage(content="", tool_calls=[{"name": "set_led_state",
                                                   "args": {"state": "ON"}, "id": "step-1"}]),
                AIMessage(content="灯亮了"),
            ]), tool_schemas={"set_led_state": SetLedState},
            build_messages=lambda current: [HumanMessage(content=current.goal)],
            execute=execute, record=record,
            checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
            effect_ledger=FileEffectLedger(tmp_path / "effects"))
    events = _events(tmp_path / "logs")
    names = [event["event"] for event in events]
    assert names.index("model.request") < names.index("tool.dispatch")
    assert names.index("tool.dispatch") < names.index("tool.result") < names.index("run.finished")
    assert "checkpoint.saved" in names
    assert all(event.get("trace_id") == trace_id for event in events)
    assert recorded[0][1] == "pending"
    assert run.status == "ok"
    assert run.decision_draft == "灯亮了"


@pytest.mark.asyncio
async def test_model_failure_never_dispatches_tool(tmp_path):
    configure_logging("api", log_dir=tmp_path / "logs")
    run = GraphRun("run_timeout", "session", "move", time.time() + 10,
                   trace_id="tr_fedcba9876543210fedcba9876543210", agent_id="go2")

    async def execute(*_args):
        raise AssertionError("tool must not run")

    await run_role_graph(
        run, model=Model([TimeoutError("upstream timeout")]),
        tool_schemas={"set_led_state": SetLedState},
        build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=execute, checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    names = [event["event"] for event in _events(tmp_path / "logs")]
    assert "model.error" in names
    assert "tool.dispatch" not in names
    assert run.status == "error"
