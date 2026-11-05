"""Describe one observed ESP32 task through a checkpointed lamp Graph."""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.replies import factual_fallback
from cloud.agents.lamp.prompts import persona_reply_prompt, valid_lamp_reply
from cloud.agents.common.runner import GraphRun
from cloud.agents.lamp.personality.profile import get_profile
from cloud.observability import emit_agent_event


class ReplyState(TypedDict, total=False):
    task_id: str
    goal: str
    action: str
    params: dict
    unit_id: str
    session_id: str
    trace_id: str
    parent_run_id: str
    status: str
    physical: dict
    response: str
    event_id: str
    error: str
    persona_error: bool


def build_physical_reply_graph(*, agent, device_state, registry, publish,
                               checkpointer: FileCheckpointSaver):
    async def observe(state: ReplyState) -> ReplyState:
        unit_id = state["unit_id"]
        if agent is None:
            return {"status": "unknown", "error": "lamp_brain_unavailable"}
        card = registry.get_card(unit_id)
        if not card or not card.get("online"):
            return {"status": "rejected", "error": "lamp_offline"}
        physical = None
        until = time.monotonic() + 2
        while physical is None and time.monotonic() < until:
            physical = device_state.get_task_result(state["task_id"])
            if physical is None:
                await asyncio.sleep(0.05)
        if physical is None:
            return {"status": "unknown", "error": "physical_result_unavailable"}
        status = str(physical.get("result") or "unknown")
        if status not in {"ok", "accepted", "executing", "timeout", "unknown",
                          "blocked", "rejected", "cancelled", "error"}:
            status = "unknown"
        return {"status": status, "physical": physical}

    async def compose(state: ReplyState) -> ReplyState:
        status = state["status"]
        if state.get("error"):
            return {"response": factual_fallback(state["unit_id"], status)}
        run = GraphRun(
            state["task_id"], state["session_id"], state["goal"], time.time() + 15,
            steps=[{"step_id": state["task_id"], "tool": state["action"],
                    "params": state["params"], "result": state["physical"]}],
            status=status, trace_id=state["trace_id"],
            parent_run_id=state["parent_run_id"], agent_id=state["unit_id"],
            origin_kind="user_delegated",
        )
        try:
            profile = get_profile()
            prompt = persona_reply_prompt(run, profile)
            emit_agent_event("model.request", agent_id=state["unit_id"],
                             run_id=state["task_id"], trace_id=state["trace_id"],
                             input=prompt, stage="physical_reply")
            model = agent._llm_for_run() if hasattr(agent, "_llm_for_run") else agent._llm
            reply = await model.ainvoke([HumanMessage(content=prompt)])
            answer = reply.content
            emit_agent_event("model.response", agent_id=state["unit_id"],
                             run_id=state["task_id"], trace_id=state["trace_id"],
                             output=answer, stage="physical_reply")
            if not valid_lamp_reply(answer, run):
                raise ValueError("invalid_persona_reply")
            return {"response": answer.strip()}
        except Exception as exc:
            emit_agent_event("model.error", agent_id=state["unit_id"],
                             run_id=state["task_id"], trace_id=state["trace_id"],
                             error=type(exc).__name__, stage="physical_reply")
            return {"response": factual_fallback(state["unit_id"], status, run),
                    "persona_error": True}

    async def publish_node(state: ReplyState) -> ReplyState:
        facts = dict(agent_id=state["unit_id"], session_id=state["session_id"],
                     status=state["status"], run_id=state["task_id"],
                     trace_id=state["trace_id"],
                     parent_run_id=state["parent_run_id"],
                     step_id=state["task_id"], source="delegated_skill")
        if state.get("persona_error"):
            publish("reply_error", **facts)
        event = publish("agent_reply", **facts, text=state["response"])
        return {"event_id": event["event_id"]}

    graph = StateGraph(ReplyState)
    graph.add_node("observe", observe)
    graph.add_node("compose", compose)
    graph.add_node("publish", publish_node)
    graph.add_edge(START, "observe")
    graph.add_edge("observe", "compose")
    graph.add_edge("compose", "publish")
    graph.add_edge("publish", END)
    return graph.compile(checkpointer=checkpointer)


async def run_physical_reply(*, task_id: str, goal: str, action: str,
                             params: dict, trace_id: str, parent_run_id: str,
                             unit_id: str, session_id: str,
                             agent, device_state, registry, publish,
                             checkpointer: FileCheckpointSaver | None = None) -> dict[str, Any]:
    saver = checkpointer or FileCheckpointSaver()
    thread_id = f"lamp:physical_reply:{session_id}:{task_id}"
    config = {"configurable": {"thread_id": thread_id}}
    graph = build_physical_reply_graph(
        agent=agent, device_state=device_state, registry=registry,
        publish=publish, checkpointer=saver)
    async with saver.async_run_lock(thread_id):
        previous = saver.get_tuple(config)
        if previous:
            values = previous.checkpoint.get("channel_values", {})
            if values.get("event_id") and values.get("task_id") == task_id:
                return {"result": values["status"], "response": values["response"],
                        "event_id": values["event_id"],
                        **({"error": values["error"]} if values.get("error") else {})}
        state = await graph.ainvoke({
            "task_id": task_id, "goal": goal, "action": action, "params": params,
            "trace_id": trace_id, "parent_run_id": parent_run_id,
            "unit_id": unit_id, "session_id": session_id,
        }, config, durability="sync")
    return {"result": state["status"], "response": state["response"],
            "event_id": state["event_id"],
            **({"error": state["error"]} if state.get("error") else {})}
