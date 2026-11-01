"""Checkpointed Go2 daily summary over the existing Markdown memory files."""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
import tempfile
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.go2.memory import daily_summary as memory
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.prompt_contract import build_prompt
from cloud.go2.execution import require_persist
from cloud.observability import emit_agent_event


class SummaryState(TypedDict, total=False):
    run_id: str
    trace_id: str
    source_date: str
    status: str
    summary: str
    episode_count: int
    source_hash: str


def _bounded_episodes(source_date: str, episodes_dir: Path | None) -> tuple[str, int]:
    episodes = memory._get_episodes_for_date(source_date, episodes_dir)
    count = len(episodes)
    if count > memory._SUMMARY_INPUT_EVENTS:
        episodes = episodes[:10] + episodes[-(memory._SUMMARY_INPUT_EVENTS - 10):]
    if not episodes:
        return "", 0
    limit = max(40, memory._SUMMARY_INPUT_CHARS // len(episodes) - 1)
    return "\n".join(line[:limit] for line in episodes)[:memory._SUMMARY_INPUT_CHARS], count


def _write_if_absent(path: Path, source_date: str, summary: str) -> str:
    existing = memory.get_summary(source_date, path.parent)
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".summary-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(f"<!-- source_date: {source_date} -->\n{summary}")
            target.flush()
            os.fsync(target.fileno())
        # The same date has one writer in the Go2 service. A pre-existing file
        # wins after a process restart, preserving the first completed summary.
        existing = memory.get_summary(source_date, path.parent)
        if existing is not None:
            return existing
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return summary
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def build_daily_summary_graph(model: Any, *, episodes_dir: Path | None = None,
                              base_dir: Path | None = None,
                              checkpointer: FileCheckpointSaver | None = None):
    """Read retained episodes, generate one bounded summary, then save Markdown."""

    async def load(state: SummaryState) -> SummaryState:
        source_date = state["source_date"]
        existing = memory.get_summary(source_date, base_dir)
        if existing is not None:
            return {"status": "existing", "summary": existing}
        text, count = _bounded_episodes(source_date, episodes_dir)
        if not text:
            return {"status": "empty", "episode_count": 0}
        return {"status": "generate", "episode_count": count,
                "source_hash": hashlib.sha256(text.encode()).hexdigest()}

    async def route_load(state: SummaryState) -> str:
        return "generate" if state["status"] == "generate" else END

    async def generate(state: SummaryState) -> SummaryState:
        source_date = state["source_date"]
        text, count = _bounded_episodes(source_date, episodes_dir)
        if not text or hashlib.sha256(text.encode()).hexdigest() != state["source_hash"]:
            return {"status": "source_changed"}
        bundle = build_prompt(
            "daily_summary", personality=get_profile()["prompt"],
            user_input=f"请概括来源日期 {source_date} 的记录。", memory=text,
            memory_sources=(f"episode:{source_date}",), budget_chars=7000,
        )
        emit_agent_event("model.request", agent_id="go2", stage="daily_summary",
                         source_date=source_date, run_id=state["run_id"],
                         trace_id=state["trace_id"],
                         input={"system": bundle.system, "selected_memory": text,
                                "human": bundle.human})
        try:
            reply = await model.ainvoke([
                SystemMessage(content=bundle.system), HumanMessage(content=bundle.human)])
            summary = str(reply.content).strip()
            if not summary:
                raise ValueError("empty_summary")
        except Exception as exc:
            emit_agent_event("model.error", agent_id="go2", stage="daily_summary",
                             source_date=source_date, run_id=state["run_id"],
                             trace_id=state["trace_id"], error=type(exc).__name__)
            return {"status": "error"}
        emit_agent_event("model.response", agent_id="go2", stage="daily_summary",
                         source_date=source_date, run_id=state["run_id"],
                         trace_id=state["trace_id"],
                         output={"private_memory_ref": str(memory._summary_file(source_date, base_dir)),
                                 "sha256": hashlib.sha256(summary.encode()).hexdigest(),
                                 "size": len(summary), "redacted_reason": "private_memory"})
        return {"status": "save", "summary": summary, "episode_count": count}

    async def route_generate(state: SummaryState) -> str:
        return "save" if state["status"] == "save" else END

    async def save(state: SummaryState) -> SummaryState:
        require_persist("write a daily summary")
        source_date = state["source_date"]
        path = memory._summary_file(source_date, base_dir)
        result = _write_if_absent(path, source_date, state["summary"])
        return {"status": "ok", "summary": result}

    graph = StateGraph(SummaryState)
    graph.add_node("load", load)
    graph.add_node("generate", generate)
    graph.add_node("save", save)
    graph.add_edge(START, "load")
    graph.add_conditional_edges("load", route_load, {"generate": "generate", END: END})
    graph.add_conditional_edges("generate", route_generate, {"save": "save", END: END})
    graph.add_edge("save", END)
    return graph.compile(checkpointer=checkpointer)


async def run_daily_summary_graph(source_date: str, *, model: Any,
                                  episodes_dir: Path | None = None,
                                  base_dir: Path | None = None,
                                  checkpointer: FileCheckpointSaver | None = None) -> SummaryState:
    date.fromisoformat(source_date)
    require_persist("write a daily summary")
    saver = checkpointer or FileCheckpointSaver()
    graph = build_daily_summary_graph(
        model, episodes_dir=episodes_dir, base_dir=base_dir,
        checkpointer=saver)
    thread_id = f"go2:daily_summary:{source_date}"
    run_id = "go2_summary_" + source_date.replace("-", "")
    async with saver.async_run_lock(thread_id):
        return await graph.ainvoke(
            {"source_date": source_date, "run_id": run_id,
             "trace_id": "tr_" + run_id},
            {"configurable": {"thread_id": thread_id}}, durability="sync")
