"""Go2 summary Graph preserves Markdown and avoids repeat generation."""

import json
import time

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.go2.workflows import daily_summary as workflow
from cloud.go2.execution import ExecutionContext, use_context


class Model:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return AIMessage(content="今天观察了客厅，也和灯打了招呼。")


@pytest.mark.asyncio
async def test_summary_graph_reuses_existing_markdown(tmp_path, monkeypatch):
    source_date = "2026-10-30"
    episodes = tmp_path / "episodes"
    episodes.mkdir()
    (episodes / f"{source_date}.jsonl").write_text(
        json.dumps({"ts": time.time(), "content": "客厅有人经过"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    summaries = tmp_path / "summaries"
    events = []
    monkeypatch.setattr(workflow, "emit_agent_event",
                        lambda kind, **fields: events.append((kind, fields)))
    monkeypatch.setattr(workflow, "get_profile", lambda: {"prompt": "活泼的 Go2"})
    model = Model()
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    context = ExecutionContext("summary-session", "admin", True, expires_at=int(time.time()) + 60)
    with use_context(context):
        first = await workflow.run_daily_summary_graph(
            source_date, model=model, episodes_dir=episodes,
            base_dir=summaries, checkpointer=saver)
        second = await workflow.run_daily_summary_graph(
            source_date, model=model, episodes_dir=episodes,
            base_dir=summaries, checkpointer=saver)
    assert first["status"] == "ok"
    assert second["status"] == "existing"
    assert first["summary"] == second["summary"]
    assert model.calls == 1
    response_event = next(fields for kind, fields in events if kind == "model.response")
    assert response_event["output"]["redacted_reason"] == "private_memory"
    assert first["summary"] not in str(response_event)
    assert (summaries / f"{source_date}.md").read_text(encoding="utf-8") == (
        f"<!-- source_date: {source_date} -->\n{first['summary']}")


@pytest.mark.asyncio
async def test_summary_graph_empty_day_never_calls_model(tmp_path):
    model = Model()
    with use_context(ExecutionContext("summary-session", "admin", True)):
        result = await workflow.run_daily_summary_graph(
            "2026-10-30", model=model, episodes_dir=tmp_path / "episodes",
            base_dir=tmp_path / "summaries",
            checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"))
    assert result["status"] == "empty"
    assert model.calls == 0

