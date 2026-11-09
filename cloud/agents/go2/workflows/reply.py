"""Checkpointed factual reply for Go2 rule and navigation actions."""

from __future__ import annotations

import time
from typing import Any, TypedDict

from langgraph.graph import START, END, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.replies import valid_reply_format, factual_fallback, finalize_reply
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
        observation = state.get("observation") or ""
        profile = profile_loader()
        instructions = (f"{reply_persona(profile)}\n"
            "你是刚观察完周围的机器狗。用第一人称和自己的性格说一句简短发现，"
            "像自然聊天，不要照抄观察报告或使用项目符号。只谈观测中确实提到的东西；"
            "画面不能确认的身份、距离和安全性不要补造。不要说已到「探索点」。"
            "只输出要说的话。" if observation else persona_reply_prompt(run, profile))

        def validate(answer, value):
            return (valid_reply_format(answer, value) and (not observation or
                (len(answer) <= 160 and "\n" not in answer and "**" not in answer)))

        def fallback(value):
            return ("我刚看了看：" + observation.split("；", 1)[0].rstrip("。") + "。"
                    if observation else factual_fallback("go2", status, value))

        await finalize_reply(run, model=model, instructions=instructions,
                             validate=validate, fallback=fallback,
                             context={"observation": observation} if observation else None)
        return {"answer": run.response}

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
