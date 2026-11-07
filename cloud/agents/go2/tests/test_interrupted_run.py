"""Interrupted Go2 conversations release admission without replaying effects."""

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.go2.run_view import Go2RunView


@tool
def motion() -> str:
    """Fake physical effect, never connected to a device."""
    raise AssertionError("declaration only")


@tool
def ask_user(question: str) -> str:
    """Ask a question."""
    return question


class Model:
    def __init__(self, message=None):
        self.message = message

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        assert self.message is not None, "recovery must not invoke a model"
        return self.message


@pytest.fixture
def view(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    return Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                      index=FileRunIndex(tmp_path / "index"))


async def abandon_motion(view, *, result_before_crash=None):
    run = GraphRun("old", "session", "find owl", time.time() + 60, agent_id="go2")
    view.register(run.run_id, run.session_id)
    calls = []
    entered = asyncio.Event()

    async def execute(name, params, step_id):
        calls.append(step_id)
        if result_before_crash:
            FileEffectLedger().mark_result("go2", thread_id_for("go2", "session"),
                                           "old", step_id, result_before_crash)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(run_role_graph(
        run, model=Model(), tool_schemas={"motion": motion},
        build_messages=lambda _: [], execute=execute,
        initial_tool=("motion", {}), checkpointer=view.checkpointer))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return run, calls


@pytest.mark.asyncio
async def test_reopened_orphan_is_unknown_pinned_and_not_replayed(view):
    _, calls = await abandon_motion(view)
    reopened = Go2RunView(checkpointer=FileCheckpointSaver(view.checkpointer.root),
                          index=FileRunIndex(view.index.root))
    assert reopened.session_busy("new", "session")
    async with reopened.index.async_session_lock("go2", "session"):
        recovered = await reopened.reconcile_interrupted("session")
    assert recovered.status == "unknown"
    assert recovered.steps[0]["result"]["result"] == "unknown"
    assert not reopened.session_busy("new", "session")
    assert calls == ["old:initial"]
    assert reopened.index.get("old", "session")["checkpoint_id"]

    # A new run can complete, while the old outcome stays addressable by ID.
    new = GraphRun("new", "session", "hello", time.time() + 60, agent_id="go2")
    reopened.register("new", "session")
    await run_role_graph(new, model=Model(AIMessage(content="hello")), tool_schemas={},
                         build_messages=lambda _: [], execute=lambda *_a: pytest.fail("replayed"),
                         checkpointer=reopened.checkpointer)
    assert new.status == "ok"
    assert reopened.get("old", "session").status == "unknown"
    assert calls == ["old:initial"]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [{"result": "ok", "receipt": "observed"},
                                     {"result": "cancelled", "reason": "manual_stop"}])
async def test_recovery_preserves_ledger_receipt_without_redispatch(view, receipt):
    _, calls = await abandon_motion(view, result_before_crash=receipt)
    async with view.index.async_session_lock("go2", "session"):
        recovered = await view.reconcile_interrupted("session")
    assert recovered.steps[0]["result"] == receipt
    assert recovered.steps[0]["dispatch_status"] == "observed"
    assert calls == ["old:initial"]


@pytest.mark.asyncio
async def test_explicit_cancellation_does_not_claim_effect_stopped(view):
    run, _ = await abandon_motion(view)
    async with view.index.async_session_lock("go2", "session"):
        recovered = await view.reconcile_interrupted("session", cancelled=run)
    assert recovered.status == "cancelled"
    assert recovered.steps[0]["result"]["result"] == "unknown"
    assert FileEffectLedger().read("go2", thread_id_for("go2", "session"),
                                   "old", "old:initial")["result"] is None
    assert view.get("old", "session").status == "cancelled"


@pytest.mark.asyncio
async def test_live_graph_lock_prevents_recovery(view):
    await abandon_motion(view)
    async with view.index.async_session_lock("go2", "session"):
        async with view.checkpointer.async_run_lock(thread_id_for("go2", "session")):
            pending = asyncio.create_task(view.reconcile_interrupted("session"))
            await asyncio.sleep(.1)
            assert not pending.done()
            assert view.get("old", "session").status == "executing"
        assert (await asyncio.wait_for(pending, 2)).status == "unknown"


@pytest.mark.asyncio
async def test_clarification_remains_busy_until_deadline(view, monkeypatch):
    now = time.time()
    run = GraphRun("question", "session", "where", now + 60, agent_id="go2")
    view.register("question", "session")
    await run_role_graph(run, model=Model(AIMessage(content="", tool_calls=[
        {"name": "ask_user", "args": {"question": "where?"}, "id": "q"}])),
        tool_schemas={"ask_user": ask_user}, build_messages=lambda _: [],
        execute=lambda *_a: pytest.fail("unexpected dispatch"), checkpointer=view.checkpointer)
    async with view.index.async_session_lock("go2", "session"):
        assert await view.reconcile_interrupted("session") is None
        assert view.session_busy("new", "session")
        monkeypatch.setattr("cloud.agents.go2.run_view.time.time", lambda: now + 61)
        assert (await view.reconcile_interrupted("session")).status == "timeout"
    assert not view.session_busy("new", "session")
