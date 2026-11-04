"""Checkpointed factual reply for Go2 rule and navigation actions."""

from __future__ import annotations

import time
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import START, END, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.replies import valid_persona_reply, factual_fallback
from cloud.agents.go2.prompts import persona_reply_prompt


class ReplyState(TypedDict, total=False):
    run_id: str
    action: str
    reason: str
    params: dict[str, Any]
    result: dict[str, Any]
    answer: str


async def run_autonomous_reply_graph(*, run_id: str, action: str, reason: str,
                                     params: dict, result: dict, model,
                                     profile_loader) -> str:
    saver = FileCheckpointSaver()

    async def reply(state: ReplyState) -> ReplyState:
        status = str(state["result"].get("result") or "unknown")
        run = GraphRun(state["run_id"], "", state["reason"], time.time() + 15,
                       steps=[{"step_id": state["run_id"], "tool": state["action"],
                               "params": state["params"], "result": state["result"]}],
                       status=status, agent_id="go2", origin_kind="agent_autonomous")
        try:
            profile = profile_loader()
            response = await model.ainvoke([HumanMessage(content=persona_reply_prompt(run, profile))])
            answer = response.content
            if not valid_persona_reply(answer, run):
                raise ValueError("invalid_persona_reply")
        except Exception:
            answer = factual_fallback("go2", status, run)
        return {"answer": answer}

    graph = StateGraph(ReplyState)
    graph.add_node("reply", reply)
    graph.add_edge(START, "reply")
    graph.add_edge("reply", END)
    compiled = graph.compile(checkpointer=saver)
    thread_id = "go2:autonomous_reply:" + run_id
    async with saver.async_run_lock(thread_id):
        output = await compiled.ainvoke(
            {"run_id": run_id, "action": action, "reason": reason,
             "params": params, "result": result},
            {"configurable": {"thread_id": thread_id}}, durability="sync")
    return output["answer"]
