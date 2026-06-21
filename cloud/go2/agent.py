"""Go2 指令智能体：Planner → Executor 两节点 LangGraph 图。"""
import asyncio
import json
import logging
import re
import sys as _sys
from typing import TypedDict

logger = logging.getLogger(__name__)

_GO2_THOUGHT_TOPIC = "ssm/agents/go2/thought"

GO2_LLM_UNAVAILABLE_REPLY = (
    "Go2 的小脑袋暂时连不上云端，刚才没法可靠理解指令。"
    "你稍后再喊我，或者先用页面上的动作按钮。"
)
GO2_AGENT_UNAVAILABLE_REPLY = (
    "Go2 这边的对话入口暂时不稳，我没把指令送进小脑袋。"
    "你稍后再试一下。"
)


def _get_mqtt_client():
    """获取 API 层共享 MQTT 客户端。"""
    from cloud.api.main import get_mqtt_client
    return get_mqtt_client()


def _publish_thought(payload: dict) -> None:
    """发布 Go2 人格化回复到 thought topic。"""
    client = _get_mqtt_client()
    if client:
        client.publish(_GO2_THOUGHT_TOPIC, json.dumps(payload, ensure_ascii=False))


from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from cloud.go2.agentcore.soul import get_system_prompt, ensure_yesterday_evolved
from cloud.go2.agentcore.memory.episode import episode_memory, EventType
from cloud.go2.agentcore.memory import daily_summary as summary_mod

from cloud.go2.connection import go2
from cloud.go2.agentcore.tools.tools import TOOL_FN_MAP, TOOL_DESCRIPTIONS, get_text_llm


class Go2AgentState(TypedDict):
    session_id:     str
    user_msg:       str
    skill_id:       str
    params:         dict
    memory_context: str
    planned_tools:  list
    tool_results:   list
    response_text:  str
    early_exit:     bool
    llm_available:  bool
    error:          str


_SKILL_TO_TOOL = {
    "go2_navigate": "go2_navigate_to",
    "go2_chat":     None,   # chat 走 LLM，无对应工具函数
}


async def planner_node(state: Go2AgentState) -> Go2AgentState:
    # 若上游已确定 skill_id，直接跳过 LLM 重规划
    if state.get("skill_id"):
        skill_id = state["skill_id"]
        # 将 card skill_id 映射到 TOOL_FN_MAP 键名；None 表示需要 LLM 处理
        tool_name = _SKILL_TO_TOOL.get(skill_id, skill_id)
        if tool_name is None:
            # go2_chat 等无对应工具的 skill 退回 LLM 规划
            pass
        else:
            logger.info("[Go2/Agent] skill_id=%s → tool=%s 直接路由，跳过规划", skill_id, tool_name)
            return {
                **state,
                "planned_tools": [{"tool": tool_name, "params": state.get("params") or {}}],
                "early_exit":    False,
            }

    if not go2.is_connected:
        logger.info("[Go2/Agent] Go2 未连接，提前退出")
        return {**state, "planned_tools": [], "early_exit": True,
                "response_text": "Go2 当前未连接，请先通过「连接」按钮建立连接。",
                "error": "go2_not_connected"}

    # 懒触发昨日摘要和性格演化（后台，不阻塞规划）
    asyncio.create_task(summary_mod.ensure_yesterday_summary())
    asyncio.create_task(ensure_yesterday_evolved())

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
        f"你是 Go2 机器狗控制智能体。根据指令生成工具调用列表。\n"
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
    try:
        llm = get_text_llm()
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        content = resp.content.strip()
        logger.info("[Go2/Agent] LLM 原始输出: %s", content)
        content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
        idx_s, idx_e = content.find("["), content.rfind("]")
    except Exception as exc:
        logger.warning("[Go2/Agent] planner llm error: %s", exc)
        return {
            **state,
            "memory_context": memory_context,
            "planned_tools": [],
            "tool_results": [],
            "response_text": GO2_LLM_UNAVAILABLE_REPLY,
            "early_exit": True,
            "llm_available": False,
            "error": "llm_unavailable",
        }
    try:
        tools_raw = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else []
    except Exception:
        tools_raw = []
    planned = [t for t in tools_raw if isinstance(t, dict) and "tool" in t]
    logger.info("[Go2/Agent] 规划工具调用 (%d 个): %s", len(planned), json.dumps(planned, ensure_ascii=False))
    return {**state, "memory_context": memory_context, "planned_tools": planned, "early_exit": False}


async def _generate_response(user_msg: str, memory_context: str, results: list) -> tuple[str, bool]:
    if not results:
        prompt = (
            f"【记忆上下文】\n{memory_context}\n\n"
            f"用户问：'{user_msg}'\n"
            f"用机器狗 Go2 的第一人称口吻回答，像在和主人说话。没有相关记忆就直接说不记得了。"
        )
    else:
        results_text = "\n".join(f"- {r['tool']}: {r['result']}" for r in results)
        prompt = (
            f"用户说：'{user_msg}'\n"
            f"执行结果：\n{results_text}\n"
            f"用机器狗 Go2 的第一人称口吻用一句话回应，像在和主人说话，不提工具名或技术细节。"
        )
    try:
        resp = await get_text_llm().ainvoke([
            SystemMessage(content=get_system_prompt()),
            HumanMessage(content=prompt),
        ])
        return resp.content.strip(), True
    except Exception as exc:
        logger.warning("[Go2/Agent] response llm error: %s", exc)
        if results:
            return "动作我已经做完了，不过云端大脑刚才没把结果整理成一句话。你看页面状态就能确认。", False
        return GO2_LLM_UNAVAILABLE_REPLY, False


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

    response_text, reply_llm_available = await _generate_response(
        state["user_msg"], state.get("memory_context", ""), results
    )
    logger.info("[Go2/Agent] 最终回复: %s", response_text)
    error = state.get("error", "")
    if not reply_llm_available and not results:
        error = "llm_unavailable"
    return {
        **state,
        "tool_results": results,
        "response_text": response_text,
        "llm_available": state.get("llm_available", True) and reply_llm_available,
        "error": error,
    }


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


async def run_agent(
    session_id: str,
    message: str,
    skill_id: str | None = None,
    params: dict | None = None,
) -> dict:
    """执行 Go2 智能体。

    若提供 skill_id，planner_node 将跳过 LLM 规划直接路由到对应工具。
    """
    episode_memory.add(EventType.USER_COMMAND, f"用户指令：{message}")
    try:
        state = await _get_agent().ainvoke({
            "session_id":     session_id,
            "user_msg":       message,
            "skill_id":       skill_id or "",
            "params":         params or {},
            "memory_context": "",
            "planned_tools":  [],
            "tool_results":   [],
            "response_text":  "",
            "early_exit":     False,
            "llm_available":  True,
            "error":          "",
        })
    except Exception as exc:
        logger.warning("[Go2/Agent] run_agent error: %s", exc)
        return {
            "result": "error",
            "response": GO2_AGENT_UNAVAILABLE_REPLY,
            "actions_taken": [],
            "llm_available": False,
            "error": "agent_unavailable",
        }
    if state["response_text"] and not state.get("early_exit"):
        _publish_thought({"type": "think", "text": state["response_text"]})
    action_failed = any(
        str(r.get("result", "")).startswith(("执行失败", "未知工具"))
        for r in state["tool_results"]
    )
    result = "error" if state.get("error") or action_failed else "ok"
    return {
        "result":        result,
        "response":      state["response_text"],
        "actions_taken": [r["tool"] for r in state["tool_results"]],
        "llm_available": state.get("llm_available", True),
        **({"error": state["error"]} if state.get("error") else {}),
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
        "skill_id":       "",
        "params":         {},
        "llm_available":  True,
        "error":          "",
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

    response_text, _ = await _generate_response(message, state.get("memory_context", ""), results)
    logger.info("[Go2/Agent] 最终回复: %s", response_text)
    episode_memory.add(EventType.AGENT_RESPONSE, f"Go2 回复：{response_text}")

    yield {"type": "response", "text": response_text}
    yield {"type": "done"}
