"""The API can retrieve historical replies from checkpoints by run ID."""

import asyncio
import time

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.views import load_run, pin_completed_run


@tool
def read_state() -> str:
    """Read current state."""
    return "ready"


@tool
def ask_user(question: str) -> str:
    """Ask for a missing detail."""
    return question


class FinalModel:
    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="草稿")


def test_historical_run_view_survives_next_request(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    index = FileRunIndex(tmp_path / "index")
    thread_id = thread_id_for("go2", "ses")

    async def execute(run_id):
        index.put_if_absent(run_id, "go2", "ses", thread_id)
        current = GraphRun(run_id, "ses", "你好", time.time() + 30, agent_id="go2")

        def finalize(run):
            run.response = f"最终回复 {run_id}"

        return await run_role_graph(
            current, model=FinalModel(), tool_schemas={"read_state": read_state},
            build_messages=lambda run: [HumanMessage(content=run.goal)],
            execute=lambda *_args: {"result": "ok"}, checkpointer=saver,
            effect_ledger=FileEffectLedger(tmp_path / "effects"), finalize=finalize,
        )

    asyncio.run(execute("first"))
    pin_completed_run("first", "ses", "go2", index=index, checkpointer=saver)
    asyncio.run(execute("second"))
    assert load_run("first", "ses", "go2", index=index, checkpointer=saver).response == "最终回复 first"
    assert load_run("second", "ses", "go2", index=index, checkpointer=saver).response == "最终回复 second"
    assert load_run("first", "other", "go2", index=index, checkpointer=saver) is None


def test_interrupted_run_view_reports_question(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    index = FileRunIndex(tmp_path / "index")
    index.put_if_absent("clarify", "go2", "ses", thread_id_for("go2", "ses"))

    class AskingModel(FinalModel):
        async def ainvoke(self, _messages):
            return AIMessage(content="", tool_calls=[{
                "name": "ask_user", "args": {"question": "去哪里？"}, "id": "ask-1"
            }])

    asyncio.run(run_role_graph(
        GraphRun("clarify", "ses", "带我去", time.time() + 30, agent_id="go2"),
        model=AskingModel(), tool_schemas={"ask_user": ask_user},
        build_messages=lambda run: [HumanMessage(content=run.goal)],
        execute=lambda *_args: {"result": "ok"}, checkpointer=saver,
        effect_ledger=FileEffectLedger(tmp_path / "effects"),
    ))
    view = load_run("clarify", "ses", "go2", index=index, checkpointer=saver)
    assert view.status == "needs_clarification"
    assert view.question == "去哪里？"
