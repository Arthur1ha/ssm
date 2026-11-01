"""Bounded model assisted lookup of a stored Go2 location name."""

from __future__ import annotations

import uuid
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import START, END, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver


class LookupState(TypedDict, total=False):
    query: str
    names: list[str]
    matched: str | None


async def run_spatial_lookup(query: str, names: list[str], model) -> str | None:
    async def lookup(state: LookupState) -> LookupState:
        prompt = (
            f"用户想去的地点：「{state['query']}」\n"
            f"已知地点列表：{state['names']}\n"
            "从列表中选出最匹配的地点名，只输出地点名，无法匹配则输出 null"
        )
        response = await model.ainvoke([HumanMessage(content=prompt)])
        matched = str(response.content).strip().strip('"').strip("'")
        return {"matched": matched if matched in state["names"] else None}

    saver = FileCheckpointSaver()
    graph = StateGraph(LookupState)
    graph.add_node("lookup", lookup)
    graph.add_edge(START, "lookup")
    graph.add_edge("lookup", END)
    compiled = graph.compile(checkpointer=saver)
    thread_id = "go2:spatial_lookup:" + uuid.uuid4().hex
    async with saver.async_run_lock(thread_id):
        result = await compiled.ainvoke(
            {"query": query, "names": names},
            {"configurable": {"thread_id": thread_id}}, durability="sync")
    return result.get("matched")
