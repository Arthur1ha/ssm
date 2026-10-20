"""Go2 指令智能体：Planner → Executor 两节点 LangGraph 图。"""
import asyncio
import json
import logging
import re
import sys as _sys
from collections import deque
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
from cloud.go2.execution import ExecutionContext, SYSTEM_CONTEXT, use_context


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
    actor:          str
    persist:        bool


_session_history: dict[str, deque[dict[str, str]]] = {}
_PERSISTENT_TOOLS = {
    "go2_add_rule", "go2_tag_location", "go2_remember_person",
    "go2_add_person_face", "go2_forget_person",
}
_ADMIN_ONLY_TOOLS = {
    # The full identity library is private. Visitors may only recognize the
    # person currently in view; saved locations remain usable for navigation.
    "go2_list_people",
}


def _tool_denial(state: Go2AgentState, tool_name: str) -> str:
    if state.get("actor", "admin") != "admin" and tool_name in _ADMIN_ONLY_TOOLS:
        return "admin_only_tool_denied"
    if not state.get("persist", True) and tool_name in _PERSISTENT_TOOLS:
        return "visitor_persistence_denied"
    return ""


def _permission_reply(reason: str) -> str:
    if reason == "admin_only_tool_denied":
        return "这个请求会读取空间的长期人员或资产信息，访客会话不能访问；我只能帮你识别当前面前的人。"
    return "这个请求会修改空间的长期记忆，访客会话不能执行。"


def _audit_tool_denial(state: Go2AgentState, tool_name: str, reason: str) -> None:
    """Record policy denials without including the user's natural-language text."""
    try:
        from cloud.api.policy import audit_event
        audit_event(
            "go2_natural_language_permission_denied",
            {
                "session_id": state.get("session_id"),
                "actor": state.get("actor"),
            },
            tool=tool_name,
            reason=reason,
        )
    except Exception as exc:
        # Auditing must not turn a safe rejection into an agent outage.
        logger.error("[Go2/Security] failed to audit tool denial: %s", exc)


def _history_context(session_id: str) -> str:
    rows = _session_history.get(session_id, ())
    if not rows:
        return "（本次临时会话暂无历史）"
    return "本次临时会话：\n" + "\n".join(
        f"{row['role']}：{row['content']}" for row in rows
    )


def _remember_turn(session_id: str, role: str, content: str) -> None:
    history = _session_history.setdefault(session_id, deque(maxlen=20))
    history.append({"role": role, "content": content})


def cleanup_session(session_id: str) -> bool:
    return _session_history.pop(session_id, None) is not None


_SKILL_TO_TOOL = {
    "go2_navigate": "go2_navigate_to",
    "go2_recognize_person": "go2_recognize_person",
    "go2_chat":     None,   # chat 走 LLM，无对应工具函数
}


def _identity_context() -> str:
    from cloud.go2.agentcore.skills.identity import identity_skill
    current = identity_skill.current
    if current.status == "recognized":
        return f"当前人物：{current.name}（{current.role}，置信分 {current.score:.1f}）"
    if current.status == "candidate":
        return "当前人物：候选身份尚未连续确认"
    if current.status == "unknown":
        return "当前人物：陌生人"
    return "当前人物：未识别"


def _cached_identity_reply(user_msg: str) -> str:
    """身份缓存有效时直接回答，避免为了同一人重复调用云端。"""
    identity_queries = ("我是谁", "认识我", "知道我是谁")
    if not any(phrase in user_msg for phrase in identity_queries):
        return ""
    from cloud.go2.agentcore.skills.identity import identity_skill
    current = identity_skill.current
    if current.status != "recognized":
        return ""
    relation = "，你是我的主人" if current.role == "owner" else f"，我们的关系是 {current.role}"
    return f"我认识你，你是{current.name}{relation}。"


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
            denial = _tool_denial(state, tool_name)
            if denial:
                _audit_tool_denial(state, tool_name, denial)
                logger.warning(
                    "[Go2/Security] planner denied tool=%s actor=%s session=%s reason=%s",
                    tool_name, state.get("actor"), state.get("session_id"), denial,
                )
                return {
                    **state, "planned_tools": [], "response_text": _permission_reply(denial),
                    "early_exit": True, "error": denial,
                }
            logger.info("[Go2/Agent] skill_id=%s → tool=%s 直接路由，跳过规划", skill_id, tool_name)
            return {
                **state,
                "planned_tools": [{"tool": tool_name, "params": state.get("params") or {}}],
                "early_exit":    False,
            }

    cached_reply = _cached_identity_reply(state["user_msg"])
    if cached_reply:
        return {
            **state, "planned_tools": [], "response_text": cached_reply,
            "early_exit": True, "memory_context": _identity_context(),
        }

    if not go2.is_connected:
        logger.info("[Go2/Agent] Go2 未连接，提前退出")
        return {**state, "planned_tools": [], "early_exit": True,
                "response_text": "Go2 当前未连接，请先通过「连接」按钮建立连接。",
                "error": "go2_not_connected"}

    memory_parts = []
    if state.get("persist", False):
        # These background jobs write files and therefore only run for an
        # administrator context.
        asyncio.create_task(summary_mod.ensure_yesterday_summary())
        asyncio.create_task(ensure_yesterday_evolved())
        recent_summaries = summary_mod.get_recent_summaries(6)
        if recent_summaries:
            lines = "\n".join(f"- {s['date']}：{s['summary']}" for s in recent_summaries)
            memory_parts.append(f"过去几天摘要：\n{lines}")
        memory_parts.append(episode_memory.format_today())
    else:
        memory_parts.append(_history_context(state["session_id"]))
    memory_parts.append(_identity_context())
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
        f"3. “我是你主人，你记住了”调用 go2_remember_person，参数固定为 name=主人, role=owner\n"
        f"4. “再记住我现在这个样子”先用当前身份；若当前未确认但只有一个主人，可用 go2_list_people 后再 go2_add_person_face\n"
        f"5. 当前身份未确认时，“面前是谁/你认识我吗”调用 go2_recognize_person；普通询问绝不能登记\n"
        f"6. 删除必须有明确 person_id；不得根据含糊描述调用 go2_forget_person\n"
        f"7. 最多生成 3 个工具调用，直接输出 JSON 数组，不含解释或代码块\n"
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
    for call in planned:
        denial = _tool_denial(state, call.get("tool", ""))
        if denial:
            _audit_tool_denial(state, call.get("tool", ""), denial)
            logger.warning(
                "[Go2/Security] planner denied tool=%s actor=%s session=%s reason=%s",
                call.get("tool"), state.get("actor"), state.get("session_id"), denial,
            )
            return {
                **state, "memory_context": memory_context, "planned_tools": [],
                "tool_results": [], "response_text": _permission_reply(denial),
                "early_exit": True, "error": denial,
            }
    logger.info("[Go2/Agent] 规划工具调用 (%d 个): %s", len(planned), json.dumps(planned, ensure_ascii=False))
    return {**state, "memory_context": memory_context, "planned_tools": planned, "early_exit": False}


async def _generate_response(user_msg: str, memory_context: str, results: list) -> tuple[str, bool]:
    # Physical outcomes are authoritative; a language model cannot upgrade acceptance to completion.
    status = _execution_status(results)
    if status != "ok":
        replies = {
            "accepted": "指令已发送，尚未确认动作完成。",
            "timeout": "等待动作完成反馈超时，当前结果还不能确认。",
            "cancelled": "动作已被后续指令打断。",
            "blocked": "当前状态不允许这个动作，设备没有执行。",
            "rejected": "这个请求没有执行：当前画面或人员信息不符合要求。",
            "disabled": "人脸登记或识别功能当前没有开启。",
        }
        return replies.get(status, "动作未能完成，请查看设备状态后重试。"), True
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
        denial = _tool_denial(state, tool_name)
        if denial:
            _audit_tool_denial(state, tool_name, denial)
            logger.warning(
                "[Go2/Security] executor denied tool=%s actor=%s session=%s reason=%s",
                tool_name, state.get("actor"), state.get("session_id"), denial,
            )
            results.append({"tool": tool_name, "result": {"result": "rejected", "error": denial}})
            break
        logger.info("[Go2/Agent] 调用 %s 参数=%s", tool_name, json.dumps(params, ensure_ascii=False))
        try:
            result = await fn(**params) if asyncio.iscoroutinefunction(fn) else fn(**params)
        except Exception as exc:
            result = {"result": "blocked" if isinstance(exc, ValueError) else "error", "error": str(exc)}
        logger.info("[Go2/Agent] %s 返回: %s", tool_name, result)
        results.append({"tool": tool_name, "result": result})
        if _execution_status([results[-1]]) not in ("ok", "accepted"):
            break

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
    context: ExecutionContext | None = None,
) -> dict:
    """执行 Go2 智能体。

    若提供 skill_id，planner_node 将跳过 LLM 规划直接路由到对应工具。
    """
    context = context or ExecutionContext(session_id, SYSTEM_CONTEXT.actor, True)
    with use_context(context):
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
            "actor":          context.actor,
            "persist":        context.persist,
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
        episode_memory.add(EventType.AGENT_RESPONSE, f"Go2 回复：{state['response_text']}")
    if not context.persist:
        _remember_turn(context.session_id, "用户", message)
        _remember_turn(context.session_id, "Go2", state["response_text"])
    if state["response_text"] and not state.get("early_exit"):
        _publish_thought({"type": "think", "text": state["response_text"]})
    result = "error" if state.get("error") else _execution_status(state["tool_results"])
    return {
        "result":        result,
        "response":      state["response_text"],
        "actions_taken": [r["tool"] for r in state["tool_results"] if _execution_status([r]) == "ok"],
        "tool_results": state["tool_results"],
        "llm_available": state.get("llm_available", True),
        **({"error": state["error"]} if state.get("error") else {}),
    }


async def run_agent_stream(session_id: str, message: str, context: ExecutionContext | None = None):
    """逐步执行并 yield 事件 dict，供 SSE 流式端点使用。"""
    context = context or ExecutionContext(session_id, SYSTEM_CONTEXT.actor, True)
    with use_context(context):
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
        "actor":          context.actor,
        "persist":        context.persist,
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

        denial = _tool_denial(state, tool_name)
        if denial:
            _audit_tool_denial(state, tool_name, denial)
            logger.warning(
                "[Go2/Security] stream executor denied tool=%s actor=%s session=%s reason=%s",
                tool_name, context.actor, context.session_id, denial,
            )
            result = {"result": "rejected", "error": denial}
        elif fn is None:
            logger.warning("[Go2/Agent] 未知工具: %s", tool_name)
            result = f"未知工具: {tool_name}"
        else:
            logger.info("[Go2/Agent] 调用 %s 参数=%s", tool_name, json.dumps(params, ensure_ascii=False))
            try:
                with use_context(context):
                    result = await fn(**params) if asyncio.iscoroutinefunction(fn) else fn(**params)
            except Exception as exc:
                result = {"result": "blocked" if isinstance(exc, ValueError) else "error", "error": str(exc)}
            logger.info("[Go2/Agent] %s 返回: %s", tool_name, result)

        results.append({"tool": tool_name, "result": result})
        yield {"type": "tool_done", "tool": tool_name, "result": str(result)}
        if _execution_status([results[-1]]) not in ("ok", "accepted"):
            break

    yield {"type": "thinking", "text": "整理回复…"}

    response_text, _ = await _generate_response(message, state.get("memory_context", ""), results)
    logger.info("[Go2/Agent] 最终回复: %s", response_text)
    with use_context(context):
        episode_memory.add(EventType.AGENT_RESPONSE, f"Go2 回复：{response_text}")
    if not context.persist:
        _remember_turn(context.session_id, "用户", message)
        _remember_turn(context.session_id, "Go2", response_text)

    yield {"type": "response", "text": response_text}
    yield {"type": "done", "result": _execution_status(results)}


def _execution_status(results: list) -> str:
    statuses = []
    for item in results:
        result = item.get("result")
        if isinstance(result, dict):
            statuses.append(result.get("result", "error"))
        elif isinstance(result, str):
            failed = result.startswith(("执行失败", "未知工具", "Go2 未连接", "无法", "无可用", "暂无", "不支持", "未找到"))
            statuses.append("error" if failed else "ok")
        else:
            statuses.append("error")
    for status in ("error", "rejected", "disabled", "blocked", "timeout", "cancelled", "accepted", "executing"):
        if status in statuses:
            return status
    return "ok"
