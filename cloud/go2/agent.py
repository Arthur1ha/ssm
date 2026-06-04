import asyncio
import json
import re
import sys as _sys
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from cloud.go2.personality import get_system_prompt
from cloud.go2.episode_memory import episode_memory, EventType

from cloud.go2.connection import go2
from cloud.go2.tools import TOOL_FN_MAP, TOOL_DESCRIPTIONS, get_text_llm


class Go2AgentState(TypedDict):
    session_id:    str
    user_msg:      str
    planned_tools: list
    tool_results:  list
    response_text: str
    early_exit:    bool


async def planner_node(state: Go2AgentState) -> Go2AgentState:
    if not go2.is_connected:
        return {**state, "planned_tools": [], "early_exit": True,
                "response_text": "Go2 当前未连接，请先通过「连接」按钮建立连接。"}

    prompt = (
        f"你是 Go2 机器狗控制智能体。根据用户指令生成工具调用列表。\n"
        f"{TOOL_DESCRIPTIONS}\n\n"
        f"用户指令：{state['user_msg']}\n\n"
        f"规则：\n"
        f"1. 涉及视觉问题（看到什么/有没有人/描述场景）必须调用 go2_observe\n"
        f"2. 最多生成 3 个工具调用\n"
        f"3. 直接输出 JSON 数组，不含解释或代码块\n"
        f"示例：[{{\"tool\": \"go2_sport\", \"params\": {{\"cmd\": \"StandUp\"}}}}]"
    )
    resp = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
    content = resp.content.strip()
    content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
    idx_s, idx_e = content.find("["), content.rfind("]")
    try:
        tools_raw = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else []
    except Exception:
        tools_raw = []
    planned = [t for t in tools_raw if isinstance(t, dict) and "tool" in t]
    return {**state, "planned_tools": planned, "early_exit": False}


async def executor_node(state: Go2AgentState) -> Go2AgentState:
    if state.get("early_exit"):
        return state

    results = []
    for call in state["planned_tools"]:
        tool_name = call.get("tool", "")
        params = call.get("params", {})
        fn = TOOL_FN_MAP.get(tool_name)
        if fn is None:
            results.append({"tool": tool_name, "result": f"未知工具: {tool_name}"})
            continue
        try:
            result = await fn(**params) if asyncio.iscoroutinefunction(fn) else fn(**params)
        except Exception as exc:
            result = f"执行失败: {exc}"
        results.append({"tool": tool_name, "result": result})

    results_text = "\n".join(f"- {r['tool']}: {r['result']}" for r in results)
    prompt = (
        f"用户说：'{state['user_msg']}'\n"
        f"执行结果：\n{results_text}\n"
        f"用 1 句简短中文告诉用户结果，语气自然，不提技术细节。"
    )
    try:
        resp = await get_text_llm().ainvoke([
            SystemMessage(content=get_system_prompt()),
            HumanMessage(content=prompt),
        ])
        response_text = resp.content.strip()
    except Exception:
        response_text = "指令已执行完成。"

    return {**state, "tool_results": results, "response_text": response_text}


def _build_agent():
    _mod = _sys.modules[__name__]

    async def _planner_wrapper(state):
        return await _mod.planner_node(state)

    async def _executor_wrapper(state):
        return await _mod.executor_node(state)

    g = StateGraph(Go2AgentState)
    g.add_node("planner",  _planner_wrapper)
    g.add_node("executor", _executor_wrapper)
    g.set_entry_point("planner")
    g.add_edge("planner",  "executor")
    g.add_edge("executor", END)
    return g.compile()


_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


async def run_agent(session_id: str, message: str) -> dict:
    episode_memory.add(EventType.USER_COMMAND, f"用户指令：{message}")
    state = await _get_agent().ainvoke({
        "session_id":    session_id,
        "user_msg":      message,
        "planned_tools": [],
        "tool_results":  [],
        "response_text": "",
        "early_exit":    False,
    })
    return {
        "response":      state["response_text"],
        "actions_taken": [r["tool"] for r in state["tool_results"]],
    }
