"""Go2 指令智能体：Planner → Executor 两节点 LangGraph 图。"""
import asyncio
import json
import logging
import re
import sys as _sys
from typing import TypedDict

logger = logging.getLogger(__name__)

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from cloud.go2.agentcore.soul import get_system_prompt
from cloud.go2.agentcore.memory.episode import episode_memory, EventType
from cloud.go2.agentcore.memory import daily_summary as summary_mod
from cloud.go2.agentcore import soul_evolution

from cloud.go2.connection import go2
from cloud.go2.agentcore.tools.tools import TOOL_FN_MAP, TOOL_DESCRIPTIONS, get_text_llm


class Go2AgentState(TypedDict):
    session_id:     str
    user_msg:       str
    memory_context: str
    planned_tools:  list
    tool_results:   list
    response_text:  str
    early_exit:     bool


async def planner_node(state: Go2AgentState) -> Go2AgentState:
    if not go2.is_connected:
        logger.info("[Go2/Agent] Go2 未连接，提前退出")
        return {**state, "planned_tools": [], "early_exit": True,
                "response_text": "Go2 当前未连接，请先通过「连接」按钮建立连接。"}

    # 懒触发昨日摘要和性格演化（后台，不阻塞规划）
    asyncio.create_task(summary_mod.ensure_yesterday_summary())
    asyncio.create_task(soul_evolution.ensure_yesterday_evolved())

    # 构建记忆上下文：今天原始记录 + 最近 6 天摘要
    recent_summaries = summary_mod.get_recent_summaries(6)
    today_ctx = episode_memory.format_today()
    memory_parts = []
    if recent_summaries:
        lines = "\n".join(f"- {s['date']}：{s['summary']}" for s in recent_summaries)
        memory_parts.append(f"过去几天摘要：\n{lines}")
    memory_parts.append(today_ctx)
    memory_context = "\n\n".join(memory_parts)

    logger.info("[Go2/Agent] 用户指令: %s", state["user_msg"])
    prompt = (
        f"你是 Go2 机器狗控制智能体。根据用户指令生成工具调用列表。\n"
        f"{TOOL_DESCRIPTIONS}\n\n"
        f"【记忆上下文】\n{memory_context}\n\n"
        f"用户指令：{state['user_msg']}\n\n"
        f"规则：\n"
        f"1. 涉及视觉问题（看到什么/有没有人/描述场景）必须调用 go2_observe\n"
        f"2. 涉及历史记忆问题（今天去哪了/发生了什么/做了什么/最近几天）"
        f"直接从记忆上下文回答，输出 []\n"
        f"3. 最多生成 3 个工具调用\n"
        f"4. 直接输出 JSON 数组，不含解释或代码块\n"
        f"示例：[{{\"tool\": \"go2_sport\", \"params\": {{\"cmd\": \"StandUp\"}}}}]\n"
        f"无需工具时输出：[]"
    )
    llm = get_text_llm()
    resp = await llm.ainvoke([HumanMessage(content=prompt)])
    content = resp.content.strip()
    logger.info("[Go2/Agent] LLM 原始输出: %s", content)
    content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
    idx_s, idx_e = content.find("["), content.rfind("]")
    try:
        tools_raw = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else []
    except Exception:
        tools_raw = []
    planned = [t for t in tools_raw if isinstance(t, dict) and "tool" in t]
    logger.info("[Go2/Agent] 规划工具调用 (%d 个): %s", len(planned), json.dumps(planned, ensure_ascii=False))
    return {**state, "memory_context": memory_context, "planned_tools": planned, "early_exit": False}


async def executor_node(state: Go2AgentState) -> Go2AgentState:
    if state.get("early_exit"):
        return state

    results = []
    for call in state["planned_tools"]:
        tool_name = call.get("tool", "")
        params = call.get("params", {})
        fn = TOOL_FN_MAP.get(tool_name)
        if fn is None:
            logger.warning("[Go2/Agent] 未知工具: %s", tool_name)
            results.append({"tool": tool_name, "result": f"未知工具: {tool_name}"})
            continue
        logger.info("[Go2/Agent] 调用 %s 参数=%s", tool_name, json.dumps(params, ensure_ascii=False))
        try:
            result = await fn(**params) if asyncio.iscoroutinefunction(fn) else fn(**params)
        except Exception as exc:
            result = f"执行失败: {exc}"
        logger.info("[Go2/Agent] %s 返回: %s", tool_name, result)
        results.append({"tool": tool_name, "result": result})

    # 无工具调用 → 纯记忆问答：LLM 基于记忆上下文直接回答
    if not results:
        prompt = (
            f"【记忆上下文】\n{state.get('memory_context', '')}\n\n"
            f"用户问：'{state['user_msg']}'\n"
            f"根据记忆上下文，用自然的中文回答用户。如果记忆中没有相关信息，如实说不记得。"
        )
    else:
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

    logger.info("[Go2/Agent] 最终回复: %s", response_text)
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
        "session_id":     session_id,
        "user_msg":       message,
        "memory_context": "",
        "planned_tools":  [],
        "tool_results":   [],
        "response_text":  "",
        "early_exit":     False,
    })
    return {
        "response":      state["response_text"],
        "actions_taken": [r["tool"] for r in state["tool_results"]],
    }


async def run_agent_stream(session_id: str, message: str):
    """逐步执行并 yield 事件 dict，供 SSE 流式端点使用。"""
    episode_memory.add(EventType.USER_COMMAND, f"用户指令：{message}")

    base_state = {
        "session_id":     session_id,
        "user_msg":       message,
        "memory_context": "",
        "planned_tools":  [],
        "tool_results":   [],
        "response_text":  "",
        "early_exit":     False,
    }

    yield {"type": "thinking", "text": "正在理解指令…"}
    state = await planner_node(base_state)

    if state.get("early_exit"):
        yield {"type": "response", "text": state["response_text"]}
        yield {"type": "done"}
        return

    results = []
    for call in state["planned_tools"]:
        tool_name = call.get("tool", "")
        params    = call.get("params", {})
        fn        = TOOL_FN_MAP.get(tool_name)

        yield {"type": "thinking", "text": f"执行 {tool_name}…"}

        if fn is None:
            logger.warning("[Go2/Agent] 未知工具: %s", tool_name)
            result = f"未知工具: {tool_name}"
        else:
            logger.info("[Go2/Agent] 调用 %s 参数=%s", tool_name, json.dumps(params, ensure_ascii=False))
            try:
                result = await fn(**params) if asyncio.iscoroutinefunction(fn) else fn(**params)
            except Exception as exc:
                result = f"执行失败: {exc}"
            logger.info("[Go2/Agent] %s 返回: %s", tool_name, result)

        results.append({"tool": tool_name, "result": result})
        yield {"type": "tool_done", "tool": tool_name, "result": str(result)}

    yield {"type": "thinking", "text": "整理回复…"}

    if not results:
        prompt = (
            f"【记忆上下文】\n{state.get('memory_context', '')}\n\n"
            f"用户问：'{message}'\n"
            f"根据记忆上下文，用自然的中文回答用户。如果记忆中没有相关信息，如实说不记得。"
        )
    else:
        results_text = "\n".join(f"- {r['tool']}: {r['result']}" for r in results)
        prompt = (
            f"用户说：'{message}'\n"
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

    logger.info("[Go2/Agent] 最终回复: %s", response_text)
    episode_memory.add(EventType.AGENT_RESPONSE, f"Go2 回复：{response_text}")

    yield {"type": "response", "text": response_text}
    yield {"type": "done"}
