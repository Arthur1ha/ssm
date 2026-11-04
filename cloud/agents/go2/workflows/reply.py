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
from cloud.agents.go2.personality.behavior import reply_persona


class ReplyState(TypedDict, total=False):
    run_id: str
    action: str
    reason: str
    params: dict[str, Any]
    result: dict[str, Any]
    answer: str
    observation: str


async def run_autonomous_reply_graph(*, run_id: str, action: str, reason: str,
                                     params: dict, result: dict, model,
                                     profile_loader, observation: str = "") -> str:
    saver = FileCheckpointSaver()

    async def reply(state: ReplyState) -> ReplyState:
        status = str(state["result"].get("result") or "unknown")
        run = GraphRun(state["run_id"], "", state["reason"], time.time() + 15,
                       steps=[{"step_id": state["run_id"], "tool": state["action"],
                               "params": state["params"], "result": state["result"]}],
                       status=status, agent_id="go2", origin_kind="agent_autonomous")
        try:
            profile = profile_loader()
            if state.get("observation"):
                prompt = (
                    f"{reply_persona(profile)}\n"
                    "你是刚观察完周围的机器狗。用第一人称和自己的性格说一句简短发现，"
                    "像自然聊天，不要照抄观察报告或使用项目符号。只谈下面观察中确实提到的东西；"
                    "画面不能确认的身份、距离和安全性不要补造。不要说已到「探索点」。"
                    "只输出要说的话。\n观察：" + state["observation"])
            else:
                prompt = persona_reply_prompt(run, profile)
            response = await model.ainvoke([HumanMessage(content=prompt)])
            answer = response.content
            if (not valid_persona_reply(answer, run)
                    or (state.get("observation") and
                        (len(answer) > 160 or "\n" in answer or "**" in answer
                         or "场景描述" in answer or "值得记录" in answer
                         or "探索点" in answer or "exploration target" in answer))):
                raise ValueError("invalid_persona_reply")
        except Exception:
            answer = ("我刚看了看：" + state["observation"].split("；", 1)[0].rstrip("。") + "。"
                      if state.get("observation") else factual_fallback("go2", status, run))
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
             "params": params, "result": result, "observation": observation},
            {"configurable": {"thread_id": thread_id}}, durability="sync")
    return output["answer"]
