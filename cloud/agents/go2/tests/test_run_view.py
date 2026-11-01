"""Go2 task status survives a new process opening the same JSON files."""

import time
from multiprocessing import get_context

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.go2.run_view import Go2RunView


def _hold_admission(root, entered, release):
    with FileRunIndex(root).session_lock("go2", "shared-session"):
        entered.set()
        release.wait(5)


def _acquire_admission(root, acquired):
    with FileRunIndex(root).session_lock("go2", "shared-session"):
        acquired.set()


@tool
def ask_user(question: str) -> str:
    """Ask a clarifying question."""
    return question


class Model:
    def __init__(self, answer):
        self.answer = answer

    def bind_tools(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        return self.answer


@pytest.mark.asyncio
async def test_status_reopens_from_checkpoint_in_new_view(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    checkpoints, index, effects = (tmp_path / "checkpoints", tmp_path / "index", tmp_path / "effects")
    writer = Go2RunView(checkpointer=FileCheckpointSaver(checkpoints), index=FileRunIndex(index))
    assert writer.register("go2-run-1", "session-1")
    run = GraphRun("go2-run-1", "session-1", "hello", time.time() + 30, agent_id="go2")
    await run_role_graph(run, model=Model(AIMessage(content="done")), tool_schemas={},
                         build_messages=lambda current: [HumanMessage(content=current.goal)],
                         execute=lambda *_args: {"result": "ok"},
                         checkpointer=writer.checkpointer, effect_ledger=FileEffectLedger(effects))
    reader = Go2RunView(checkpointer=FileCheckpointSaver(checkpoints), index=FileRunIndex(index))
    stored = reader.get("go2-run-1", "session-1")
    assert stored is not None
    assert stored.status == "ok"
    assert stored.decision_draft == "done"
    assert reader.get("go2-run-1", "other-session") is None
    assert not reader.register("go2-run-1", "session-1")


@pytest.mark.asyncio
async def test_clarification_is_visible_and_resumable_from_new_view(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    checkpoints, index, effects = (tmp_path / "checkpoints", tmp_path / "index", tmp_path / "effects")
    writer = Go2RunView(checkpointer=FileCheckpointSaver(checkpoints), index=FileRunIndex(index))
    writer.register("go2-run-2", "session-2")
    run = GraphRun("go2-run-2", "session-2", "go somewhere", time.time() + 30, agent_id="go2")
    model = Model(AIMessage(content="", tool_calls=[
        {"name": "ask_user", "args": {"question": "去哪里？"}, "id": "q1"}]))
    await run_role_graph(run, model=model, tool_schemas={"ask_user": ask_user},
                         build_messages=lambda current: [HumanMessage(content=current.goal)],
                         execute=lambda *_args: {"result": "ok"},
                         checkpointer=writer.checkpointer, effect_ledger=FileEffectLedger(effects))
    reader = Go2RunView(checkpointer=FileCheckpointSaver(checkpoints), index=FileRunIndex(index))
    stored = reader.get("go2-run-2", "session-2", require_head=True)
    assert stored is not None
    assert stored.status == "needs_clarification"
    assert stored.question == "去哪里？"


def test_admission_lock_serializes_two_processes(tmp_path):
    multiprocessing = get_context("fork")
    entered = multiprocessing.Event()
    release = multiprocessing.Event()
    acquired = multiprocessing.Event()
    holder = multiprocessing.Process(target=_hold_admission,
                                     args=(tmp_path / "index", entered, release))
    contender = multiprocessing.Process(target=_acquire_admission,
                                        args=(tmp_path / "index", acquired))
    holder.start()
    try:
        assert entered.wait(5)
        contender.start()
        assert not acquired.wait(0.2)
        release.set()
        assert acquired.wait(5)
        contender.join(5)
        assert contender.exitcode == 0
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join(5)
        if contender.is_alive():
            contender.terminate()
            contender.join(5)
