"""Durable task cancellation after API reload never controls the device."""

import asyncio
import importlib
import time
from dataclasses import asdict

import pytest
from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun
from cloud.agents.go2.run_view import Go2RunView, _RecoveryState
from cloud.go2.execution import ExecutionContext
from cloud.go2.tasks import ChatTaskRegistry


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    router = importlib.import_module("cloud.go2.router")
    graph = importlib.import_module("cloud.agents.go2.graph")
    tasks = importlib.import_module("cloud.go2.tasks")
    view = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                      index=FileRunIndex(tmp_path / "index"))
    registry = ChatTaskRegistry()
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setattr(graph, "get_store", lambda: view)
    monkeypatch.setattr(tasks, "chat_tasks", registry)
    monkeypatch.setattr(router, "_normalize_context", lambda value: value)

    async def forbidden(*_a, **_k):
        pytest.fail("historical cancellation must not control the device")

    monkeypatch.setattr(router, "_stop_task_motion", forbidden)
    return router, view, registry, ExecutionContext("session", "admin", True)


async def save(view, run):
    view.register(run.run_id, run.session_id)
    graph = StateGraph(_RecoveryState)
    graph.add_node("finalize", lambda _: {})
    graph.add_edge(START, "finalize")
    graph.add_edge("finalize", END)
    await graph.compile(checkpointer=view.checkpointer).aupdate_state(
        {"configurable": {"thread_id": thread_id_for("go2", run.session_id)}},
        {"run": asdict(run), "decision": {}, "turns": 0, "effects": 0},
        as_node="finalize")


@pytest.mark.asyncio
async def test_reload_cancel_closes_conversation_and_preserves_unknown_effect(recovery):
    router, view, _, context = recovery
    run = GraphRun("orphan", "session", "owl", time.time() - 600, agent_id="go2",
                   steps=[{"step_id": "effect", "tool": "go2_find_object", "params": {},
                           "effectful": True, "dispatch_status": "pending", "result": None}])
    await save(view, run)
    # Read-only status must not mutate the persisted run, let alone the robot.
    assert (await router.go2_task_status("orphan", context))["result"] == "executing"
    assert view.get("orphan", "session").status == "executing"
    cancelled = await router.go2_task_cancel("orphan", context)
    assert cancelled["result"] == "cancelled"
    assert cancelled["tool_results"][0]["result"]["result"] == "unknown"
    assert "stop" not in cancelled
    assert not view.session_busy("next", "session")
    assert (await router.go2_task_cancel("orphan", context)) == cancelled


@pytest.mark.asyncio
async def test_cancel_terminal_history_returns_receipt_and_session_isolation(recovery):
    router, view, _, context = recovery
    await save(view, GraphRun("done", "session", "hello", time.time() + 30,
                              agent_id="go2", status="ok", response="done"))
    view.pin("done", "session")
    result = await router.go2_task_cancel("done", context)
    assert result["result"] == "ok"
    assert result["response"] == "done"
    for task_id, actor in [("missing", context),
                           ("done", ExecutionContext("another", "admin", True))]:
        with pytest.raises(HTTPException) as exc:
            await router.go2_task_cancel(task_id, actor)
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_old_unpinned_snapshot_cannot_cancel_new_head(recovery):
    router, view, _, context = recovery
    await save(view, GraphRun("old", "session", "old", time.time() + 30, agent_id="go2"))
    await save(view, GraphRun("new", "session", "new", time.time() + 30, agent_id="go2"))
    result = await router.go2_task_cancel("old", context)
    assert result["result"] == "unknown"
    assert result["error"] == "historical_run_unconfirmed"
    assert view.get("new", "session", require_head=True).status == "executing"


@pytest.mark.asyncio
async def test_memory_task_keeps_existing_stop_path(recovery, monkeypatch):
    router, _, registry, context = recovery
    entered = asyncio.Event()
    stops = []

    async def live():
        entered.set()
        await asyncio.Event().wait()

    async def stop(owner):
        stops.append(owner.session_id)
        return {"result": "ok", "confirmation": "test_observation"}

    monkeypatch.setattr(router, "_stop_task_motion", stop)
    record = registry.start("live", "session", "f", time.time() + 30, live)
    await asyncio.wait_for(entered.wait(), 2)
    result = await router.go2_task_cancel("live", context)
    assert result["result"] == "cancelled"
    assert result["stop"]["confirmation"] == "test_observation"
    assert record.task.done()
    assert stops == ["session"]
