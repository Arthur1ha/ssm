"""Go2 trait evolution as an isolated, checkpointed LangGraph workflow."""

from __future__ import annotations

from datetime import date
import hashlib
from typing import Any, Callable, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.go2.personality.profile import (
    evolve_profile, get_evolution_metadata, get_profile,
)
from cloud.go2.execution import require_persist
from cloud.observability import emit_agent_event


class TraitDelta(BaseModel):
    curiosity: int = Field(default=0, ge=-5, le=5)
    extraversion: int = Field(default=0, ge=-5, le=5)
    boldness: int = Field(default=0, ge=-5, le=5)


class EvolutionState(TypedDict, total=False):
    run_id: str
    trace_id: str
    source_date: str
    summary: str
    status: str
    delta: dict[str, int]
    traits: dict[str, int]


def build_evolution_graph(
    model: Any, *,
    profile_loader: Callable[[], dict] = get_profile,
    metadata_loader: Callable[[], dict] = get_evolution_metadata,
    commit: Callable[[dict, str], dict] = evolve_profile,
    checkpointer: FileCheckpointSaver | None = None,
):
    decision_model = model.with_structured_output(TraitDelta, method="function_calling")

    async def gate(state: EvolutionState) -> EvolutionState:
        if (metadata_loader().get("last_evolved") or "") >= state["source_date"]:
            return {"status": "existing", "traits": profile_loader()["traits"]}
        if not state.get("summary", "").strip():
            return {"status": "empty"}
        return {"status": "decide"}

    async def route_gate(state: EvolutionState) -> str:
        return "decide" if state["status"] == "decide" else END

    async def decide(state: EvolutionState) -> EvolutionState:
        profile = profile_loader()
        traits = profile["traits"]
        prompt = (
            f"来源日期：{state['source_date']}。当前好奇心 {traits['curiosity']}，"
            f"外向性 {traits['extraversion']}，大胆程度 {traits['boldness']}。"
            "根据当天经历决定三项变化；通常 -5 到 +5，缺乏依据时为 0。"
            f"\n经历摘要：{state['summary']}"
        )
        emit_agent_event("model.request", agent_id="go2", stage="trait_evolution",
                         source_date=state["source_date"], run_id=state["run_id"],
                         trace_id=state["trace_id"],
                         input={"selected_memory": state["summary"]})
        try:
            raw = await decision_model.ainvoke([
                SystemMessage(content="你是 Go2 特质演化系统。只依据真实经历调整特质。"),
                HumanMessage(content=prompt)])
            delta = TraitDelta.model_validate(raw).model_dump()
        except Exception as exc:
            emit_agent_event("model.error", agent_id="go2", stage="trait_evolution",
                             source_date=state["source_date"], run_id=state["run_id"],
                             trace_id=state["trace_id"], error=type(exc).__name__)
            return {"status": "error"}
        emit_agent_event("model.response", agent_id="go2", stage="trait_evolution",
                         source_date=state["source_date"], run_id=state["run_id"],
                         trace_id=state["trace_id"], output=delta)
        return {"status": "commit", "delta": delta}

    async def route_decide(state: EvolutionState) -> str:
        return "commit" if state["status"] == "commit" else END

    async def commit_node(state: EvolutionState) -> EvolutionState:
        require_persist("evolve Go2 traits")
        updated = commit(state["delta"], state["source_date"])
        return {"status": "ok", "traits": updated["traits"]}

    graph = StateGraph(EvolutionState)
    graph.add_node("gate", gate)
    graph.add_node("decide", decide)
    graph.add_node("commit", commit_node)
    graph.add_edge(START, "gate")
    graph.add_conditional_edges("gate", route_gate, {"decide": "decide", END: END})
    graph.add_conditional_edges("decide", route_decide, {"commit": "commit", END: END})
    graph.add_edge("commit", END)
    return graph.compile(checkpointer=checkpointer)


async def run_evolution_graph(source_date: str, summary: str, *, model: Any,
                              checkpointer: FileCheckpointSaver | None = None,
                              profile_loader: Callable[[], dict] = get_profile,
                              metadata_loader: Callable[[], dict] = get_evolution_metadata,
                              commit: Callable[[dict, str], dict] = evolve_profile,
                              checkpoint_scope: str = "") -> EvolutionState:
    date.fromisoformat(source_date)
    require_persist("evolve Go2 traits")
    saver = checkpointer or FileCheckpointSaver()
    graph = build_evolution_graph(
        model, profile_loader=profile_loader, metadata_loader=metadata_loader,
        commit=commit, checkpointer=saver)
    scope = hashlib.sha256(checkpoint_scope.encode()).hexdigest()[:12] if checkpoint_scope else "default"
    thread_id = f"go2:trait_evolution:{scope}:{source_date}"
    run_id = "go2_evolution_" + source_date.replace("-", "")
    async with saver.async_run_lock(thread_id):
        return await graph.ainvoke(
            {"source_date": source_date, "summary": summary,
             "run_id": run_id, "trace_id": "tr_" + run_id},
            {"configurable": {"thread_id": thread_id}}, durability="sync")
