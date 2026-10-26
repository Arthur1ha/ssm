"""Go2 指令智能体：Planner → Executor 两节点 LangGraph 图。"""
import asyncio
import json
import logging
import re
import sys as _sys
import time
from collections import deque
from typing import TypedDict

logger = logging.getLogger(__name__)

GO2_LLM_UNAVAILABLE_REPLY = (
    "Go2 的对话服务暂时不可用，刚才没法可靠理解指令。"
    "你稍后再喊我，或者先用页面上的动作按钮。"
)
GO2_AGENT_UNAVAILABLE_REPLY = (
    "Go2 这边的对话入口暂时不稳，我没把指令送进小脑袋。"
    "你稍后再试一下。"
)


from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from cloud.go2.agentcore.soul import get_system_prompt, ensure_yesterday_evolved
from cloud.go2.agentcore.memory.episode import episode_memory, EventType
from cloud.go2.agentcore.memory import daily_summary as summary_mod
from cloud.go2.agentcore.memory.recall import recall_memory
from cloud.go2.agentcore.prompts import build_prompt

from cloud.go2.connection import go2
from cloud.go2.agentcore.tools.tools import TOOL_FN_MAP
from cloud.go2.agentcore.tools.registry import get_tool_spec, tool_descriptions
from cloud.go2.agentcore.tools.executor import execute_tool
from cloud.go2.agentcore.llm import get_text_llm
from cloud.go2.card import get_card_skill
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
    execution_context: ExecutionContext


_session_history: dict[str, deque[dict[str, str]]] = {}
_pending_clarifications: dict[tuple[str, str], dict] = {}
_CLARIFICATION_TTL_S = 180
def _tool_denial(state: Go2AgentState, tool_name: str) -> str:
    spec = get_tool_spec(tool_name)
    if spec is None:
        return "unknown_tool"
    if state.get("actor", "admin") not in spec.roles:
        return "admin_only_tool_denied"
    if not state.get("persist", True) and spec.persists:
        return "visitor_persistence_denied"
    return ""


def _permission_reply(reason: str) -> str:
    if reason == "unknown_tool":
        return "请求中的工具不存在。"
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
    _pending_clarifications.pop((session_id, "go2"), None)
    return _session_history.pop(session_id, None) is not None


def _identity_context() -> str:
    from cloud.go2.agentcore.perception.identity import identity_skill
    current = identity_skill.current
    if current.status == "recognized":
        return f"当前人物：{current.name}（{current.role}，置信分 {current.score:.1f}）"
    if current.status == "candidate":
        return "当前人物：候选身份尚未连续确认"
    if current.status == "unknown":
        return "当前人物：陌生人"
    return "当前人物：未识别"


def _space_context() -> str:
    """Read the shared public space snapshot for this model turn."""
    try:
        from cloud.api.main import _esp32_state
        from cloud.space.context import get_space_context
        snapshot = get_space_context("go2", state=_esp32_state)
    except Exception as exc:
        logger.warning("[Go2/Agent] space snapshot unavailable: %s", type(exc).__name__)
        return "空间现状暂不可用"
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))


def _cached_identity_reply(user_msg: str) -> str:
    """身份缓存有效时直接回答，避免为了同一人重复调用云端。"""
    identity_queries = ("我是谁", "认识我", "知道我是谁")
    if not any(phrase in user_msg for phrase in identity_queries):
        return ""
    from cloud.go2.agentcore.perception.identity import identity_skill
    current = identity_skill.current
    if current.status != "recognized":
        return ""
    relation = "，你是我的主人" if current.role == "owner" else f"，我们的关系是 {current.role}"
    return f"我认识你，你是{current.name}{relation}。"


def _clarification(state: Go2AgentState) -> dict | None:
    """Keep ambiguous short movement goals scoped to one Go2 conversation."""
    message = state["user_msg"].strip()
    key = (state["session_id"], "go2")
    pending = _pending_clarifications.get(key)
    if pending and pending["expires_at"] < time.monotonic():
        _pending_clarifications.pop(key, None)
        pending = None
    directions = {"前": "向前", "后": "向后", "左": "向左", "右": "向右"}
    direction_match = re.search(r"向([前后左右])|([前后左右])进|([前后左右])退", message)
    direction = directions.get(next((part for part in direction_match.groups() if part), "")) if direction_match else None
    duration_match = re.search(r"(\d+(?:\.\d+)?)\s*秒", message)
    duration = duration_match.group(1) if duration_match else None
    step_request = bool(re.search(r"(?:移动|走|前进|后退).*(?:两步|2步|几步)", message))
    if step_request:
        pending = {"expires_at": time.monotonic() + _CLARIFICATION_TTL_S,
                   "direction": direction, "duration": None}
        _pending_clarifications[key] = pending
    if pending:
        is_continuation = step_request or bool(direction or duration) and bool(
            re.fullmatch(r"(?:向?[前后左右](?:进|退|走|移动)?|\d+(?:\.\d+)?\s*秒|向?[前后左右].*\d+(?:\.\d+)?\s*秒)", message)
        )
        if not is_continuation:
            _pending_clarifications.pop(key, None)
            return None
        pending["direction"] = direction or pending["direction"]
        pending["duration"] = duration or pending["duration"]
        missing = [field for field in ("direction", "duration") if not pending[field]]
        if missing:
            question = "我只能按时长近似移动，无法确认实际走了几步。"
            if "direction" in missing:
                question += "请告诉我方向。"
            if "duration" in missing:
                question += "请告诉我移动时长（秒）。"
            return {"result": "needs_clarification", "question": question,
                    "missing_fields": missing}
        state["user_msg"] = f"{pending['direction']}移动{pending['duration']}秒，按时长近似移动，实际位移未确认"
        _pending_clarifications.pop(key, None)
    return None


async def planner_node(state: Go2AgentState) -> Go2AgentState:
    clarification = _clarification(state)
    if clarification:
        return {**state, "planned_tools": [], "response_text": clarification["question"],
                "early_exit": True, "result_override": "needs_clarification",
                "question": clarification["question"], "missing_fields": clarification["missing_fields"]}
    # 若上游已确定 skill_id，直接跳过 LLM 重规划
    if state.get("skill_id"):
        skill_id = state["skill_id"]
        card_skill = get_card_skill(skill_id)
        if card_skill is None:
            return {**state, "planned_tools": [], "response_text": f"未知能力: {skill_id}",
                    "early_exit": True, "error": "unknown_skill", "result_override": "error"}
        tool_name = card_skill["tool_name"]
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

    if state.get("persist", False):
        # These background jobs write files and therefore only run for an
        # administrator context.
        asyncio.create_task(summary_mod.ensure_yesterday_summary())
        asyncio.create_task(ensure_yesterday_evolved())
    recalled = recall_memory(
        state["user_msg"], actor=state.get("actor", "admin"),
        session_id=state["session_id"],
        session_history=_session_history.get(state["session_id"], ()),
        current_state=_identity_context(), persist=state.get("persist", True),
    )
    memory_context = recalled.text

    logger.info("[Go2/Agent] planning session=%s message_chars=%d", state["session_id"], len(state["user_msg"]))
    prompt = build_prompt(
        "planner", personality=get_system_prompt(), user_input=state["user_msg"], memory=memory_context,
        tools=tool_descriptions(), device_state=f"fsm={getattr(go2, 'fsm_state', 'unknown')}; space={_space_context()}",
        memory_sources=recalled.sources, tool_version="go2-tools-v2",
    )
    logger.info("[Go2/Prompt] %s", prompt.metadata)
    try:
        llm = get_text_llm()
        resp = await llm.ainvoke([SystemMessage(content=prompt.system), HumanMessage(content=prompt.human)])
        content = resp.content.strip()
        logger.info("[Go2/Agent] planner output_chars=%d", len(content))
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
    except Exception as exc:
        logger.warning("[Go2/Agent] planner llm error: %s", type(exc).__name__)
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
        tools_raw = json.loads(content)
        # Some compatible chat models return one call as an object even when
        # the prompt asks for an array. Keep the same validation path.
        if isinstance(tools_raw, dict):
            tools_raw = [tools_raw]
        if isinstance(tools_raw, list):
            normalized = []
            for call in tools_raw:
                # A peer request sometimes arrives as its parameters without
                # the tool envelope. Only this complete, known schema is safe
                # to identify; execution still passes through the tool gateway.
                if (isinstance(call, dict) and "tool" not in call
                        and {"target_unit_id", "skill_id", "params"} <= set(call)
                        and set(call) <= {"target_unit_id", "skill_id", "params", "request_id"}
                        and isinstance(call["target_unit_id"], str)
                        and isinstance(call["skill_id"], str)
                        and isinstance(call["params"], dict)):
                    call = {"tool": "request_peer_skill", "params": call}
                normalized.append(call)
            tools_raw = normalized
        if not isinstance(tools_raw, list) or any(
            not isinstance(t, dict) or not isinstance(t.get("tool"), str)
            or not isinstance(t.get("params", {}), dict) for t in tools_raw
        ):
            raise ValueError("planner_output_invalid_shape")
        if len(tools_raw) > 3:
            raise ValueError("tool_call_limit_exceeded")
    except (ValueError, json.JSONDecodeError) as exc:
        error = "tool_call_limit_exceeded" if str(exc) == "tool_call_limit_exceeded" else "invalid_planner_json"
        logger.warning("[Go2/Plan/Error] session=%s reason=%s detail=%s output=%s",
                       state["session_id"], error, str(exc),
                       json.dumps(content[:2000], ensure_ascii=False))
        return {**state, "planned_tools": [], "response_text": "我暂时无法可靠解析这个指令，请换种说法。",
                "early_exit": True, "error": error, "result_override": "error"}
    planned = tools_raw
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
    logger.info("[Go2/Plan] session=%s tools=%s", state["session_id"],
                json.dumps(planned, ensure_ascii=False))
    return {**state, "memory_context": memory_context, "planned_tools": planned, "early_exit": False}


def _responder_results(results: list) -> str:
    """Keep confirmed peer outcomes while hiding transport bookkeeping from Go2's reply."""
    visible = []
    for item in results:
        if item.get("tool") == "request_peer_skill" and isinstance(item.get("result"), dict):
            outcome = item["result"]
            detail = {"result": outcome.get("result", "error")}
            if detail["result"] == "ok":
                detail["completion"] = "目标设备已反馈执行成功"
            if outcome.get("error"):
                detail["error"] = outcome["error"]
            visible.append({"operation": "同伴设备动作", "outcome": detail})
        else:
            visible.append(item)
    return json.dumps({"overall_result": _execution_status(results), "steps": visible},
                      ensure_ascii=False, default=str)


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
            "needs_clarification": "请补充完成这个动作所需的信息。",
        }
        return replies.get(status, "动作未能完成，请查看设备状态后重试。"), True
    prompt = build_prompt("responder", personality=get_system_prompt(),
                          user_input=user_msg, memory=memory_context,
                          tool_results=_responder_results(results))
    logger.info("[Go2/Prompt] %s", prompt.metadata)
    try:
        resp = await get_text_llm().ainvoke([
            SystemMessage(content=prompt.system),
            HumanMessage(content=prompt.human),
        ])
        reply = resp.content.strip()
        if any(item.get("tool") == "request_peer_skill" for item in results) and (
            re.search(r"(?:尚未|还没|未能|没有).{0,18}(?:确认|完成)", reply)
            or re.search(r"(?:request_id|task_id|unit_id|ESP32|请求编号|灯控模块)", reply, re.I)
        ):
            logger.warning("[Go2/Reply] peer completion reply contradicted result or exposed internals")
            return "汪，交给我的事已经办妥啦。", True
        return reply, True
    except Exception as exc:
        logger.warning("[Go2/Agent] response llm error: %s", type(exc).__name__)
        if results:
            return "动作我已经做完了，不过云端大脑刚才没把结果整理成一句话。你看页面状态就能确认。", False
        return GO2_LLM_UNAVAILABLE_REPLY, False


async def _execute_calls(state: Go2AgentState, context: ExecutionContext):
    accepted_motion = False
    for call in state.get("planned_tools", [])[:3]:
        name = call["tool"]
        params = call.get("params", {})
        spec = get_tool_spec(name)
        is_stop = name == "go2_sport" and params.get("cmd") == "StopMove"
        if accepted_motion and spec and spec.motion and not is_stop:
            result = {"result": "blocked", "error": "previous_motion_unconfirmed",
                      "message": "前一个运动尚未确认完成，未执行后续运动"}
        else:
            result = await execute_tool(name, params, context=context)
        item = {"tool": name, "result": result}
        logger.info("[Go2/Tool] session=%s tool=%s status=%s error=%s",
                    state["session_id"], name, result.get("result"), result.get("error", "-"))
        yield item
        if spec and spec.motion and result["result"] == "accepted" and not is_stop:
            accepted_motion = True
        if result["result"] not in ("ok", "accepted") or (name == "request_peer_skill" and result["result"] != "ok"):
            break


async def executor_node(state: Go2AgentState) -> Go2AgentState:
    if state.get("early_exit"):
        return state

    context = state.get("execution_context") or ExecutionContext(
        state["session_id"], state.get("actor", "admin"), state.get("persist", True)
    )
    results = [item async for item in _execute_calls(state, context)]

    response_text, reply_llm_available = await _generate_response(
        state["user_msg"], state.get("memory_context", ""), results
    )
    logger.info("[Go2/Agent] response_chars=%d", len(response_text))
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
    logger.info("[Go2/Chat/User] session=%s text=%s", session_id,
                json.dumps(message, ensure_ascii=False))
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
            "execution_context": context,
        })
        except Exception as exc:
            logger.warning("[Go2/Agent] run_agent error: %s", type(exc).__name__)
            logger.info("[Go2/Chat/Reply] session=%s result=error text=%s", session_id,
                        json.dumps(GO2_AGENT_UNAVAILABLE_REPLY, ensure_ascii=False))
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
    result = state.get("result_override") or ("error" if state.get("error") else _execution_status(state["tool_results"]))
    logger.info("[Go2/Chat/Reply] session=%s result=%s text=%s", session_id, result,
                json.dumps(state["response_text"], ensure_ascii=False))
    return {
        "result":        result,
        "response":      state["response_text"],
        "actions_taken": [r["tool"] for r in state["tool_results"] if _execution_status([r]) == "ok"],
        "tool_results": state["tool_results"],
        "llm_available": state.get("llm_available", True),
        **({"question": state["question"], "missing_fields": state["missing_fields"]}
           if result == "needs_clarification" else {}),
        **({"error": state["error"]} if state.get("error") else {}),
    }


async def run_agent_stream(session_id: str, message: str, context: ExecutionContext | None = None):
    """逐步执行并 yield 事件 dict，供 SSE 流式端点使用。"""
    context = context or ExecutionContext(session_id, SYSTEM_CONTEXT.actor, True)
    logger.info("[Go2/Chat/User] session=%s text=%s", session_id,
                json.dumps(message, ensure_ascii=False))
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
        logger.info("[Go2/Chat/Reply] session=%s result=%s text=%s", session_id,
                    state.get("result_override") or ("error" if state.get("error") else "ok"),
                    json.dumps(state["response_text"], ensure_ascii=False))
        yield {"type": "response", "text": state["response_text"]}
        yield {"type": "done", "result": state.get("result_override") or
               ("error" if state.get("error") else "ok"),
               **({"question": state["question"], "missing_fields": state["missing_fields"]}
                  if state.get("result_override") == "needs_clarification" else {})}
        return

    results = []
    async for item in _execute_calls(state, context):
        results.append(item)
        yield {"type": "tool_done", "tool": item["tool"], "result": item["result"]}

    yield {"type": "thinking", "text": "整理回复…"}

    response_text, _ = await _generate_response(message, state.get("memory_context", ""), results)
    logger.info("[Go2/Agent] response_chars=%d", len(response_text))
    logger.info("[Go2/Chat/Reply] session=%s result=%s text=%s", session_id,
                _execution_status(results), json.dumps(response_text, ensure_ascii=False))
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
