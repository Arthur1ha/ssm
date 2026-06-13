"""graph.py — 用户意图编排图（card-driven）。

Planner 是唯一大脑：一次 LLM 调用同时完成分类（act/chat/define_rule）与
规划/回答/建规则，输出带 route 字段的 JSON。图按 route 条件分支：
  - act         → Dispatcher → Evaluator → Responder → END
  - chat        → ChatNode → END
  - define_rule → RuleBuilderNode → END
Dispatcher 按 card.transport.kind 分支：mqtt 走 MQTT 发布，http 走线程池非阻塞；
Evaluator 只对 mqtt task 轮询 result topic，http 结果由 Dispatcher 已填好。
"""

import os
import re
import json
import time as _time
import logging
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END

import tools as _t

logger = logging.getLogger("orchestrator")


# ── 对话历史 ─────────────────────────────────────────────────────
_conversation_history: list = []
_MAX_HISTORY = 10


def _format_env_state(sensors: dict, actuators: dict) -> str:
    """把传感器和执行器快照格式化为可读字符串，注入 Planner prompt。"""
    lines = []
    for uid, data in sensors.items():
        s = data.get("state") or data.get("event") or {}
        lines.append(f"- {uid}：ISM={s.get('ism', '?')}")
    for uid, data in actuators.items():
        s = data.get("state", {})
        lines.append(f"- {uid}：ISM={s.get('ism', '?')}")
    return "\n".join(lines) if lines else "暂无环境数据"


def _append_history(user_msg: str, response: str):
    """追加一轮对话记录，超出 _MAX_HISTORY 时移除最旧一条。"""
    _conversation_history.append({"user": user_msg, "assistant": response})
    while len(_conversation_history) > _MAX_HISTORY:
        _conversation_history.pop(0)


def _make_llm():
    """构建带 fallback 链的 ChatOpenAI（按 MODEL_LIST 顺序重试）。"""
    model_list_str = os.getenv("MODEL_LIST", os.getenv("MODEL", ""))
    models = [m.strip() for m in model_list_str.split(",") if m.strip()]

    base_kwargs = dict(
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0,
        timeout=30,
    )
    llms = [ChatOpenAI(model=m, **base_kwargs) for m in models]
    logger.info("[Graph] LLM fallback chain: %s", " → ".join(models))
    return llms[0].with_fallbacks(llms[1:]) if len(llms) > 1 else llms[0]


class OrchestratorState(TypedDict):
    """编排图共享状态。

    requirements 为 NLU 结果，可选（Task 5 退役 NLU 后将为空）。
    planned_tasks 每项形如 {unit_id, skill_id, task_id, params}。
    """

    session_id:    str
    user_msg:      str
    requirements:  list   # NLU 结果，可选，默认 []
    route:         str    # "act" / "chat" / "define_rule"，由 Planner 分类，默认 ""
    planned_tasks: list   # [{unit_id, skill_id, task_id, params}]
    rule:          dict   # define_rule 时填充的规则定义，默认 {}
    task_results:  dict   # task_id → result payload
    response_text: str
    early_exit:    bool    # True = feedback 已发出，跳过后续节点


# ── prompt 构建 ──────────────────────────────────────────────────

def _build_card_prompt(cards: dict, user_msg: str, requirements: list,
                        env_state: str = "", history: list = None) -> str:
    """把在线 card skills、环境状态、对话历史注入 prompt，一次 LLM 完成分类 + 规划。

    Planner 输出带 route 字段的 JSON，四选一：act / chat / define_rule。
    """
    lines = []
    for unit_id, card in cards.items():
        if not card.get("online", True):
            continue
        if not card.get("skills"):
            continue
        lines.append(f"- {unit_id}（{card.get('name', unit_id)}）：")
        for skill in card.get("skills", []):
            schema = skill.get("params_schema", {})
            props = schema.get("properties", {})
            params_desc = json.dumps(props, ensure_ascii=False)
            lines.append(f"  skill: {skill['id']}（{skill.get('name', '')}）params: {params_desc}")
    agents_str = "\n".join(lines) if lines else "无可用智能体"

    history_str = ""
    if history:
        turns = [f"  用户：{h['user']}\n  助理：{h['assistant']}" for h in history]
        history_str = "最近对话记录：\n" + "\n".join(turns) + "\n\n"

    env_str = f"当前环境状态：\n{env_state}\n\n" if env_state else ""

    return (
        f"你是 SSM 多智能体编排中枢，兼任智能家居助理。根据用户意图，输出一个 JSON 对象（不含代码块）。\n\n"
        f"{history_str}"
        f"{env_str}"
        f"用户原话：{user_msg}\n"
        f"意图解析（可能为空）：{json.dumps(requirements, ensure_ascii=False)}\n\n"
        f"可用智能体：\n{agents_str}\n\n"
        f"分类规则：\n"
        f"1. 如果用户明确要控制某个设备或调用某个智能体的技能 → route=\"act\"，输出 tasks 数组。\n"
        f"2. 如果用户宣布开始某项活动（如'我要工作了'、'开始学习'、'准备睡觉'）且当前环境状态需要调整（如灯光过暗/过亮）→ route=\"act\"，主动规划合适的任务。\n"
        f"3. 如果用户说'以后...就...'、'每次...就...'、'当...时自动...' → route=\"define_rule\"，输出 rule 对象。\n"
        f"4. 其他（问候、闲聊、纯问答、不明确指向某设备且环境无需调整）→ route=\"chat\"，输出 answer 字符串。\n\n"
        f"act 校验：unit_id 必须在可用智能体列表里，skill_id 必须在该智能体的 skill 列表里，"
        f"params 必须符合对应 skill 的 params_schema。\n"
        f"找不到合适的 unit_id/skill_id 时改用 route=\"chat\" 回答'抱歉，没有合适的设备'。\n\n"
        f"输出格式（四选一，直接输出 JSON，不含代码块或解释）：\n"
        f'- act:         {{"route": "act", "tasks": [{{"unit_id": "...", "skill_id": "...", "params": {{...}}}}]}}\n'
        f'- chat:        {{"route": "chat", "answer": "..."}}\n'
        f'- define_rule: {{"route": "define_rule", "rule": {{"name": "...", "trigger": {{"tag": "light_level|presence|sound", "event": "..."}}, "action": {{"tag": "lighting", "cmd": "SET_STATE", "params": {{...}}}}}}}}'
    )


def _parse_planner_output(content: str) -> dict:
    """从 LLM 输出中提取 JSON 对象，剥离代码块包裹。

    返回带 route 字段的 dict；解析失败返回 {}（调用方按空规划处理）。
    """
    content = content.strip()
    content = re.sub(r'```(?:json)?\n?', '', content).strip().rstrip('`').strip()
    idx_s = content.find('{')
    idx_e = content.rfind('}')
    if idx_s == -1 or idx_e == -1:
        return {}
    return json.loads(content[idx_s:idx_e + 1])


def _http_timeout_for(card: dict, skill_id: str) -> float:
    """按 skill tag 决定 HTTP 超时：含 navigation → 30s，其余 → 10s。"""
    for skill in card.get("skills", []):
        if skill.get("id") == skill_id:
            if "navigation" in skill.get("tags", []):
                return 30
            break
    return 10


# ── 节点工厂 ──────────────────────────────────────────────────────

def _make_planner_node(llm):
    """构建 Planner 节点（唯一大脑）：读 CardRegistry，一次 LLM 完成分类 + 规划。

    输出 route（act/chat/define_rule）：
      - act：校验后产出 planned_tasks；
      - chat：把 answer 写入 response_text，交给 ChatNode 发出；
      - define_rule：把 rule 写入 state，交给 RuleBuilderNode 处理。
    解析失败或注册表为空时降级为安全 chat / failed。
    """

    def planner_node(state: OrchestratorState) -> OrchestratorState:
        session_id = state["session_id"]
        _t.do_publish_feedback(session_id, "planning", "正在理解你的意图...")

        cards = _t._registry.get_all_cards() if _t._registry else {}
        online_ids = [uid for uid, c in cards.items() if c.get("online", True)]
        logger.info("[Planner] 在线 card：%s", online_ids or "（无）")
        if not cards:
            _t.do_publish_feedback(session_id, "failed",
                "抱歉，我还没有发现可用的智能体，请确认设备已上线。")
            return {**state, "route": "act", "planned_tasks": [], "early_exit": True}

        sensors   = _t._state.sensor_snapshot()   if _t._state else {}
        actuators = _t._state.actuator_snapshot() if _t._state else {}
        env_state = _format_env_state(sensors, actuators)
        prompt = _build_card_prompt(
            cards, state["user_msg"], state.get("requirements", []),
            env_state=env_state, history=list(_conversation_history),
        )

        try:
            resp = llm.invoke([HumanMessage(content=prompt)])
            out = _parse_planner_output(resp.content)
        except Exception as e:
            logger.error("[Planner] parse error: %s", e)
            out = {}

        route = out.get("route", "act")
        if route not in ("act", "chat", "define_rule"):
            route = "act"

        # ── chat：纯问答/闲聊，answer 直接交给 ChatNode 发出 ──
        if route == "chat":
            answer = out.get("answer", "") or "抱歉，我没太理解你的意思。"
            logger.info("[Planner] route=chat | 回复: %s", answer)
            return {**state, "route": "chat", "response_text": answer,
                    "planned_tasks": [], "early_exit": False}

        # ── define_rule：把规则交给 RuleBuilderNode ──
        if route == "define_rule":
            rule = out.get("rule", {}) or {}
            logger.info("[Planner] route=define_rule | 规则名: %s", rule.get("name", ""))
            return {**state, "route": "define_rule", "rule": rule,
                    "planned_tasks": [], "early_exit": False}

        # ── act（默认）：校验 tasks ──
        tasks_raw = out.get("tasks", [])
        if not isinstance(tasks_raw, list):
            tasks_raw = []

        tasks = []
        for i, t in enumerate(tasks_raw):
            if not isinstance(t, dict):
                continue
            unit_id = t.get("unit_id")
            skill_id = t.get("skill_id")
            card = cards.get(unit_id)
            if not card:
                logger.warning("[Planner] 丢弃未知 unit_id: %s", unit_id)
                continue
            if not any(s.get("id") == skill_id for s in card.get("skills", [])):
                logger.warning("[Planner] 丢弃 %s 未知 skill_id: %s", unit_id, skill_id)
                continue
            tasks.append({
                "unit_id":  unit_id,
                "skill_id": skill_id,
                "task_id":  f"{session_id}_t{i}",
                "params":   t.get("params", {}),
            })

        # tasks 校验后为空 → 退化为闲聊，避免 Dispatcher 返回冷冰冰的错误
        if not tasks:
            try:
                chat_prompt = (f"用户说：'{state['user_msg']}'。"
                               f"没有合适的设备可以执行，请用一句简短友好的中文回应用户，"
                               f"不要提技术细节。")
                chat_resp = llm.invoke([HumanMessage(content=chat_prompt)])
                answer = chat_resp.content.strip()
            except Exception:
                answer = "抱歉，暂时没有合适的设备来完成这个请求。"
            logger.info("[Planner] route=act→chat（无合适设备）| 回复: %s", answer)
            return {**state, "route": "chat", "response_text": answer,
                    "planned_tasks": [], "early_exit": False}

        logger.info("[Planner] route=act → %d 个任务:", len(tasks))
        for i, t in enumerate(tasks):
            logger.info("[Planner]   [%d] %s → %s  params=%s",
                        i, t["unit_id"], t["skill_id"], json.dumps(t["params"], ensure_ascii=False))
        return {**state, "route": "act", "planned_tasks": tasks, "early_exit": False}

    return planner_node


def _make_chat_node(llm):
    """构建 ChatNode：把 Planner 已填好的 answer（response_text）发回 PWA。

    llm 形参为统一节点工厂签名预留，本节点不调用 LLM。
    """

    def chat_node(state: OrchestratorState) -> OrchestratorState:
        session_id = state["session_id"]
        answer = state.get("response_text", "") or "抱歉，我没太理解你的意思。"
        _t.do_publish_feedback(session_id, "done", answer)
        logger.info("[Chat] → %s", answer)
        _append_history(state["user_msg"], answer)
        return state

    return chat_node


def _make_rule_builder_node():
    """构建 RuleBuilderNode：发 pending_rule 反馈，附带 rule 供 PWA 确认后生效。"""

    def rule_builder_node(state: OrchestratorState) -> OrchestratorState:
        session_id = state["session_id"]
        rule = state.get("rule", {}) or {}
        _t.do_publish_feedback(
            session_id, "pending_rule",
            f"我来帮你设置规则「{rule.get('name', '')}」，请确认后生效。",
            rule=rule,
        )
        logger.info("[RuleBuilder] pending rule=%s", rule.get("name", ""))
        return state

    return rule_builder_node


def _make_dispatcher_node():
    """构建 Dispatcher 节点：按 transport.kind 分支派发。

    mqtt task 同步发布（结果留给 Evaluator 轮询）；
    http task 并发提交线程池，统一收口，结果直接填入 task_results。
    """

    def dispatcher_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        session_id = state["session_id"]
        tasks = state["planned_tasks"]

        if not tasks:
            _t.do_publish_feedback(session_id, "failed",
                "抱歉，我没找到合适的智能体来完成这个请求。")
            return {**state, "early_exit": True}

        _t.do_publish_feedback(session_id, "executing", "正在执行...")

        results = dict(state.get("task_results", {}))
        http_jobs = []   # [(task_id, future, timeout)]
        executor = None

        for task in tasks:
            unit_id = task["unit_id"]
            card = _t._registry.get_card(unit_id) if _t._registry else None
            if not card:
                results[task["task_id"]] = {"result": "error", "task_id": task["task_id"],
                                            "error": "card_not_found"}
                continue

            skill = next((s for s in card.get("skills", []) if s.get("id") == task["skill_id"]), None)
            if not skill:
                results[task["task_id"]] = {"result": "error", "task_id": task["task_id"],
                                            "error": "skill_not_found"}
                continue

            kind = card.get("transport", {}).get("kind")

            if kind == "mqtt":
                action = skill.get("invoke", {}).get("action", "")
                _t.do_publish_task(unit_id, task["task_id"], action, task["params"], session_id)
                logger.info("[Dispatcher] ➤ mqtt  %s  %s  params=%s  task_id=%s",
                            unit_id, action,
                            json.dumps(task["params"], ensure_ascii=False), task["task_id"])

            elif kind == "http":
                endpoint = card.get("transport", {}).get("endpoint", "")
                body = {
                    "session_id": session_id,
                    "message":    skill.get("name", "") + ": " + json.dumps(task["params"], ensure_ascii=False),
                    "skill_id":   task["skill_id"],
                    "params":     task["params"],
                }
                timeout = _http_timeout_for(card, task["skill_id"])
                if executor is None:
                    executor = ThreadPoolExecutor(max_workers=8)
                fut = executor.submit(_t.do_http_dispatch, endpoint, body, timeout)
                http_jobs.append((task["task_id"], fut, timeout))
                logger.info("[Dispatcher] ➤ http   %s  %s  params=%s  timeout=%ss  task_id=%s",
                            unit_id, endpoint,
                            json.dumps(task["params"], ensure_ascii=False), timeout, task["task_id"])

            else:
                results[task["task_id"]] = {"result": "error", "task_id": task["task_id"],
                                            "error": f"unknown_transport:{kind}"}

        # 统一收口：最大超时 = 所有 http task 中最大超时（+1s 余量）
        if http_jobs:
            max_timeout = max(t for _, _, t in http_jobs) + 1
            wait([f for _, f, _ in http_jobs], timeout=max_timeout)
            for task_id, fut, _ in http_jobs:
                try:
                    results[task_id] = fut.result(timeout=0)
                except concurrent.futures.TimeoutError:
                    logger.warning("[Dispatcher] http task %s timed out", task_id)
                    results[task_id] = {"result": "timeout", "task_id": task_id}
                except Exception as e:
                    logger.warning("[Dispatcher] http task %s error: %s", task_id, e)
                    results[task_id] = {"result": "error", "task_id": task_id}
            executor.shutdown(wait=False)

        return {**state, "task_results": results}

    return dispatcher_node


def _make_evaluator_node():
    """构建 Evaluator 节点：只对 mqtt task 轮询 result topic。

    http 结果已由 Dispatcher 填入 task_results，跳过轮询。
    """

    def evaluator_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        tasks = state["planned_tasks"]
        if not tasks:
            return state

        results = dict(state.get("task_results", {}))

        # 只需轮询：transport 为 mqtt 且结果尚未到位的 task
        pending = []
        for task in tasks:
            tid = task["task_id"]
            if tid in results:
                continue
            unit_id = task["unit_id"]
            card = _t._registry.get_card(unit_id) if _t._registry else None
            kind = card.get("transport", {}).get("kind") if card else "mqtt"
            if kind == "mqtt":
                pending.append(tid)

        if pending:
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                for tid in pending:
                    if tid not in results:
                        r = _t._state.get_task_result(tid)
                        if r:
                            results[tid] = r
                if all(tid in results for tid in pending):
                    break
                _time.sleep(0.2)

        # 兜底：仍缺失的记为 timeout
        for task in tasks:
            tid = task["task_id"]
            if tid not in results:
                results[tid] = {"result": "timeout", "task_id": tid}

        for task in tasks:
            tid = task["task_id"]
            r = results.get(tid, {})
            status = r.get("result", "?")
            extra = f" error={r['error']}" if r.get("error") else ""
            logger.info("[Evaluator] %s → %s%s  (%s → %s)",
                        tid, status, extra, task["unit_id"], task["skill_id"])
        return {**state, "task_results": results}

    return evaluator_node


def _make_responder_node(llm):
    """构建 Responder 节点：根据结果汇总成一句自然语言反馈。"""

    def responder_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        session_id = state["session_id"]
        results = state["task_results"]
        if not results:
            return state

        statuses = [r.get("result", "timeout") for r in results.values()]
        ok_count = statuses.count("ok")
        stage = "done" if ok_count == len(results) else ("partial" if ok_count > 0 else "failed")

        fallbacks = {"done": "好的，指令已全部执行！", "partial": "部分指令已执行。",
                     "failed": "智能体未响应，请检查连接后重试。"}
        try:
            if stage == "done":
                prompt = (f"用户说：'{state['user_msg']}'。任务已全部执行成功。"
                          f"用1句简短中文告诉用户结果，语气自然友好，不提技术细节。")
            elif stage == "partial":
                prompt = (f"用户说：'{state['user_msg']}'。部分任务成功（{statuses}）。"
                          f"用1句简短中文说明并给出建议。")
            else:
                tried = [f"{t['unit_id']}.{t['skill_id']}({t['params']})" for t in state.get("planned_tasks", [])]
                reasons = {k: v.get("result") for k, v in results.items()}
                prompt = (f"用户说：'{state['user_msg']}'。"
                          f"系统尝试执行{tried}但失败（{reasons}）。"
                          f"用1句简短友好中文告诉用户执行失败，建议检查设备连接，不提技术细节。")
            resp = llm.invoke([HumanMessage(content=prompt)])
            text = resp.content.strip()
        except Exception:
            text = fallbacks[stage]

        _t.do_publish_feedback(session_id, stage, text)
        logger.info("[Responder] stage=%s | %s", stage, text)
        _append_history(state["user_msg"], text)
        return {**state, "response_text": text}

    return responder_node


def build_orchestrator():
    """装配 Planner（唯一大脑）+ 条件分支编排图。

    Planner 按 route 分流：
      - act         → Dispatcher → Evaluator → Responder → END
      - chat        → ChatNode → END
      - define_rule → RuleBuilderNode → END
    """
    llm = _make_llm()

    g = StateGraph(OrchestratorState)
    g.add_node("planner",      _make_planner_node(llm))
    g.add_node("dispatcher",   _make_dispatcher_node())
    g.add_node("evaluator",    _make_evaluator_node())
    g.add_node("responder",    _make_responder_node(llm))
    g.add_node("chat",         _make_chat_node(llm))
    g.add_node("rule_builder", _make_rule_builder_node())

    g.set_entry_point("planner")
    g.add_conditional_edges(
        "planner",
        lambda s: s.get("route", "act"),
        {"act": "dispatcher", "chat": "chat", "define_rule": "rule_builder"},
    )
    g.add_edge("dispatcher",   "evaluator")
    g.add_edge("evaluator",    "responder")
    g.add_edge("responder",    END)
    g.add_edge("chat",         END)
    g.add_edge("rule_builder", END)

    return g.compile()
