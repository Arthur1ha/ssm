"""Receipt replay reuses the original autonomy Graph without repeating actions."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from cloud.agents.go2.workflows.free_explore import build_motivation_graph, resume_autonomy_result


class MemorySaver(InMemorySaver):
    @asynccontextmanager
    async def async_run_lock(self, _thread):
        yield


def profile():
    return {"name": "小狗", "revision": 1, "traits": {"curiosity": 50, "extraversion": 50}, "prompt": "你是机器狗。"}


@pytest.mark.asyncio
async def test_result_branch_skips_advance_and_replays_without_inference():
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content="看到了桌腿。")
    graph = build_motivation_graph(profile_loader=profile, checkpointer=MemorySaver(), model=model)
    request = {"run_id": "scene-one", "action": "go2_observe", "result": {"result": "ok"},
               "observation": "前方有桌腿。"}
    config = {"configurable": {"thread_id": "scene-one"}}
    first = await graph.ainvoke({"reply_request": request, "curiosity": 12,
                                "current_goal": {"event_id": "original"}}, config, durability="sync")
    second = await graph.ainvoke({"reply_request": request}, config, durability="sync")
    assert first["reply_text"] == second["reply_text"] == "看到了桌腿。"
    assert first["curiosity"] == 12
    assert first["current_goal"] == {"event_id": "original"}
    assert "should_explore" not in first
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_failed_reply_is_claimed_and_not_regenerated_on_receipt_replay(monkeypatch):
    saver = MemorySaver()
    monkeypatch.setattr("cloud.agents.common.checkpoint.FileCheckpointSaver", lambda: saver)
    model = AsyncMock()
    model.ainvoke.side_effect = RuntimeError("model unavailable")
    fields = dict(run_id="motion-one", action="go2_sport", reason="", params={"cmd": "Hello"},
                  result={"result": "accepted"}, model=model, profile_loader=profile)
    first = await resume_autonomy_result(**fields)
    second = await resume_autonomy_result(**fields)
    assert first == second == {"text": "", "emotion": "neutral", "error": "reply_unavailable"}
    assert model.ainvoke.await_count == 1
