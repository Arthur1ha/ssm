"""graph.py — 用户意图编排图（card-driven）。

Planner 是唯一大脑：一次 LLM 调用同时完成分类（act/chat/define_rule）与
规划/回答/建规则，输出带 route 字段的 JSON。图按 route 条件分支：
  - act              → Dispatcher → Evaluator → Responder → END
  - chat             → ChatNode → END
  - define_rule      → RuleBuilderNode → END
  - discover_devices → DiscoveryNode → END
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
from cloud.space.registry import (
    build_adoption_candidates,
    get_adopted_cards,
    get_space_registry,
)

logger = logging.getLogger("orchestrator")


# ── 编排器自身人格 ───────────────────────────────────────────────
# 编排器是智慧空间的"管家/协调者"：温暖、简洁、像主人贴心的贴身管家。
# 只负责"框住整体计划与结果"，不复述各设备的具体动作细节（细节由设备自己的
# 台词去说），避免与设备台词重复打架。
AGENT_PERSONA = (
    "你是这个智慧空间的贴心管家，像主人身边温暖体贴的老朋友。"
    "说话简洁自然、有人情味，只负责把整体安排和结果轻轻交代一句，不啰嗦细节。"
)

LLM_UNAVAILABLE_REPLY = (
    "我这边和云端大脑的连接有点晃，刚才没法放心替你安排。"
    "你稍等我缓一口气，或者先点设备卡片直接控制。"
)


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
    route:         str    # "act" / "chat" / "define_rule" / "discover_devices"，由 Planner 分类，默认 ""
    planned_tasks: list   # [{unit_id, skill_id, task_id, params}]
    rule:          dict   # define_rule 时填充的规则定义，默认 {}
    task_results:  dict   # task_id → result payload
    response_text: str
    early_exit:    bool    # True = feedback 已发出，跳过后续节点


# ── prompt 构建 ──────────────────────────────────────────────────

def _build_card_prompt(cards: dict, user_msg: str, requirements: list,
                        env_state: str = "", history: list = None) -> str:
    """把在线 card skills、环境状态、对话历史注入 prompt，一次 LLM 完成分类 + 规划。

    Planner 输出带 route 字段的 JSON：act / chat / define_rule / discover_devices。
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
        f"你是 SSM 多智能体编排中枢，兼任智能家居管家。根据用户意图，输出一个 JSON 对象（不含代码块）。\n"
        f"人格设定（仅影响 route=chat 时 answer 的语气）：{AGENT_PERSONA}\n"
        f"当 route=chat 时，answer 用这位管家的口吻，温暖简洁、像贴心老朋友；"
        f"当 route=act 时只框住整体安排（如'都给你安排好咯'），不要复述各设备的具体动作。\n\n"
        f"{history_str}"
        f"{env_str}"
        f"用户原话：{user_msg}\n"
        f"意图解析（可能为空）：{json.dumps(requirements, ensure_ascii=False)}\n\n"
        f"已接入可调用智能体（未接入设备不在此列表中，不能用于 act 任务）：\n{agents_str}\n\n"
        f"分类规则：\n"
        f"1. 如果用户想发现、查看、接入当前空间里的新设备/新成员 → route=\"discover_devices\"。\n"
        f"2. 如果用户明确要控制某个设备或调用某个智能体的技能 → route=\"act\"，输出 tasks 数组。\n"
        f"3. 如果用户宣布到场/离开/开始某项活动（如'我回来了'、'我要工作了'、'开始学习'、'准备睡觉'、'我走了'、'客人来了'）→ route=\"act\"，同时规划所有相关任务：\n"
        f"   ① 若灯光/环境状态需要调整，加入灯光任务；\n"
        f"   ② 若 Go2 机器狗在线，加入一个对话类（conversation）任务，把用户的真实意图/目标转告它。\n"
        f"4. 如果用户说'以后...就...'、'每次...就...'、'当...时自动...' → route=\"define_rule\"，输出 rule 对象。\n"
        f"5. 其他（纯问答、闲聊、环境无需调整且无明确设备指向）→ route=\"chat\"，输出 answer 字符串。\n\n"
        f"技能路由引导（按 skill 的 tags 选择，保持通用 card-driven）：\n"
        f"- 明确的肢体动作（跳舞/站起/坐下/打招呼/握手等）→ 选带 motion 类 tag 的 skill，"
        f"params 按其 schema 填（通常是 cmd）。\n"
        f"- 要去某个地点/导航 → 选带 navigation 类 tag 的 skill，params 填目标位置（通常是 name）。\n"
        f"- 开放式目标/场景意图（如'客人来了'、'我回来了'、'去门口看看'）→ 选带 conversation 类 tag 的 skill"
        f"（如 go2_chat）。\n\n"
        f"★ 关键：conversation 类 skill（go2_chat）的 params.message 必须是【用户真实意图/目标的忠实转述原文】，"
        f"不要写成机器狗的人格台词或拟声词。下游智能体会用这段文字作为指令意图，自主规划要做的动作。\n"
        f"  例：用户'客人回来了' → message=\"客人回来了，去门口迎接打招呼\"；"
        f"用户'我回来了' → message=\"主人回来了，去门口迎接\"。\n"
        f"  反例（禁止）：message=\"汪汪我来啦\"、\"摇尾巴欢迎你\" 这类台词——会导致机器狗规划不出动作。\n\n"
        f"灯光（你是唯一调色大脑，像贴心管家那样自行拿捏什么最贴合用户当下的需要，不要套固定场景规则）：\n"
        f"- set_light_color 是精细手段：可调任意颜色与亮度（r/g/b/brightness 均为 0-255）。"
        f"当用户想要某种感受/氛围、或没有现成预设贴合时，用它，色温和明暗你自己判断。\n"
        f"- set_light_state 只有三个粗预设 OFF/DIM/BRIGHT（其中 DIM 是偏冷的白光）：仅当用户就是直白地要开/关/调暗/调亮时用。\n"
        f"- 用户不再需要灯时（要睡了、离开了）就关灯，别画蛇添足。\n\n"
        f"act 校验：unit_id 必须在已接入可调用智能体列表里，skill_id 必须在该智能体的 skill 列表里，"
        f"params 必须符合对应 skill 的 params_schema。\n"
        f"找不到合适的 unit_id/skill_id 时改用 route=\"chat\" 回答'抱歉，没有合适的设备'。\n\n"
        f"★ act 时附带 ack 字段：一句管家口吻、温暖简短的【即时确认】，表示你已领会用户意图、这就去张罗"
        f"（在动作执行前先说给用户，让 ta 立刻收到回应）。例：用户'我回来了' → "
        f"ack='欢迎回来，我这就安排大家为你接风～'。ack 是开场,不要把执行结果写进去（结果由收尾语负责）。\n\n"
        f"输出格式（四选一，直接输出 JSON，不含代码块或解释）：\n"
        f'- act:         {{"route": "act", "ack": "...", "tasks": [{{"unit_id": "...", "skill_id": "...", "params": {{...}}}}]}}\n'
        f'- chat:        {{"route": "chat", "answer": "..."}}\n'
        f'- define_rule: {{"route": "define_rule", "rule": {{"name": "...", "trigger": {{"tag": "light_level|presence|sound", "event": "..."}}, "action": {{"tag": "lighting", "cmd": "SET_STATE", "params": {{...}}}}}}}}\n'
        f'- discover_devices: {{"route": "discover_devices"}}'
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


def _looks_like_discovery_request(text: str) -> bool:
    """发现入口的可靠兜底；只决定 route，候选内容仍从 Agent Card 动态生成。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    discovery_terms = ("新成员", "可接入", "接入设备", "发现设备", "附近设备")
    return any(term in t for term in discovery_terms)


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

        discovered_cards = _t._registry.get_all_cards() if _t._registry else {}
        space_registry = get_space_registry()
        cards = get_adopted_cards(discovered_cards, space_registry)
        online_ids = [uid for uid, c in cards.items() if c.get("online", True)]
        discovered_ids = [uid for uid, c in discovered_cards.items() if c.get("online", True)]
        logger.info("[Planner] 已接入在线 card：%s", online_ids or "（无）")
        logger.info("[Planner] 已发现在线 card：%s", discovered_ids or "（无）")

        if state.get("route") == "discover_devices" or _looks_like_discovery_request(state.get("user_msg", "")):
            candidates = build_adoption_candidates(discovered_cards, space_registry)
            if candidates:
                text = "我听到几个新成员在打招呼，先把它们的名片递给你。"
            else:
                offline_candidates = build_adoption_candidates(discovered_cards, space_registry, include_offline=True)
                text = (
                    "我记得有几位成员来过，但现在没在线。等它们通电联网后，我就能把名片递给你。"
                    if offline_candidates else
                    "我暂时没听到新设备上线。你可以先给设备通电联网，等它发出名片我就能认出来。"
                )
            _t.do_publish_feedback(
                session_id,
                "discovery_candidates",
                text,
                devices=candidates,
            )
            logger.info("[Planner] route=discover_devices(pre_llm) | candidates=%d", len(candidates))
            _append_history(state["user_msg"], text)
            return {**state, "route": "discover_devices", "planned_tasks": [],
                    "response_text": text, "early_exit": True}

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
            if not out:
                raise ValueError("empty planner output")
        except Exception as e:
            logger.error("[Planner] llm/parse error: %s", e)
            return {**state, "route": "chat", "response_text": LLM_UNAVAILABLE_REPLY,
                    "planned_tasks": [], "early_exit": False}

        route = out.get("route", "act")
        if route not in ("act", "chat", "define_rule", "discover_devices"):
            route = "act"

        # ── discover_devices：只生成候选卡，能力内容来自 Agent Card，不让 LLM 编造 ──
        if route == "discover_devices":
            candidates = build_adoption_candidates(discovered_cards, space_registry)
            if candidates:
                text = "我听到几个新成员在打招呼，先把它们的名片递给你。"
            else:
                offline_candidates = build_adoption_candidates(discovered_cards, space_registry, include_offline=True)
                text = (
                    "我记得有几位成员来过，但现在没在线。等它们通电联网后，我就能把名片递给你。"
                    if offline_candidates else
                    "我暂时没听到新设备上线。你可以先给设备通电联网，等它发出名片我就能认出来。"
                )
            _t.do_publish_feedback(
                session_id,
                "discovery_candidates",
                text,
                devices=candidates,
            )
            logger.info("[Planner] route=discover_devices | candidates=%d", len(candidates))
            _append_history(state["user_msg"], text)
            return {**state, "route": "discover_devices", "planned_tasks": [],
                    "response_text": text, "early_exit": True}

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
                chat_prompt = (f"{AGENT_PERSONA}\n"
                               f"用户说：'{state['user_msg']}'。"
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

        # 即时确认：Planner 一领会意图就先回一句开场白（动作执行前），
        # 让用户立刻收到管家回应；最终结果由 Responder 收尾。
        ack = (out.get("ack") or "").strip()
        if ack:
            _t.do_publish_feedback(session_id, "ack", ack)
            logger.info("[Planner] ack → %s", ack)
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
        # 管家口吻的规则确认语（静态文案，不调 LLM，保持轻快温暖）。
        _t.do_publish_feedback(
            session_id, "pending_rule",
            f"好嘞，我来帮你记下规则「{rule.get('name', '')}」，你确认一下我就让它生效哈~",
            rule=rule,
        )
        logger.info("[RuleBuilder] pending rule=%s", rule.get("name", ""))
        return state

    return rule_builder_node


def _make_discovery_node():
    """Discovery feedback 已在 Planner 发出，本节点只负责闭合 route。"""

    def discovery_node(state: OrchestratorState) -> OrchestratorState:
        return state

    return discovery_node


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
                # message 是给下游（Go2 agent）的指令意图：
                # 优先用 params.message（Planner 已忠实转述用户真实意图），
                # 否则退回用户原话，最后才退回 skill 名+params（保证非空）。
                message = (task["params"].get("message")
                           or state.get("user_msg")
                           or (skill.get("name", "") + ": "
                               + json.dumps(task["params"], ensure_ascii=False)))
                body = {
                    "session_id": session_id,
                    "message":    message,
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

        llm_error_reply = next(
            (
                r.get("response") or r.get("reply")
                for r in results.values()
                if r.get("error") == "llm_unavailable" and (r.get("response") or r.get("reply"))
            ),
            "",
        )
        if stage == "failed" and llm_error_reply:
            _t.do_publish_feedback(session_id, stage, llm_error_reply)
            logger.info("[Responder] stage=%s | %s", stage, llm_error_reply)
            _append_history(state["user_msg"], llm_error_reply)
            return {**state, "response_text": llm_error_reply}

        fallbacks = {"done": "好的，指令已全部执行！", "partial": "部分指令已执行。",
                     "failed": "智能体未响应，请检查连接后重试。"}
        # 管家只"框住整体结果"，不复述各设备具体动作（细节由设备自己的台词去说）。
        persona_hint = (f"{AGENT_PERSONA}\n"
                        f"只用管家口吻框住整体结果（如'都给你安排好咯~''交给狗狗啦~'），"
                        f"不要复述每个设备做了什么具体动作。\n")
        try:
            if stage == "done":
                prompt = (f"{persona_hint}"
                          f"用户说：'{state['user_msg']}'。任务已全部执行成功。"
                          f"用1句简短中文告诉用户都安排好了，语气自然温暖，不提技术细节、不列动作清单。")
            elif stage == "partial":
                prompt = (f"{persona_hint}"
                          f"用户说：'{state['user_msg']}'。部分任务成功（{statuses}）。"
                          f"用1句简短中文说明大致安排好了、还有一点没成，并轻轻给个建议。")
            else:
                tried = [f"{t['unit_id']}.{t['skill_id']}({t['params']})" for t in state.get("planned_tasks", [])]
                reasons = {k: v.get("result") for k, v in results.items()}
                prompt = (f"{persona_hint}"
                          f"用户说：'{state['user_msg']}'。"
                          f"系统尝试执行{tried}但失败（{reasons}）。"
                          f"用1句简短温暖中文告诉用户这次没安排成，建议检查下设备连接，不提技术细节。")
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
      - act              → Dispatcher → Evaluator → Responder → END
      - chat             → ChatNode → END
      - define_rule      → RuleBuilderNode → END
      - discover_devices → DiscoveryNode → END
    """
    llm = _make_llm()

    g = StateGraph(OrchestratorState)
    g.add_node("planner",      _make_planner_node(llm))
    g.add_node("dispatcher",   _make_dispatcher_node())
    g.add_node("evaluator",    _make_evaluator_node())
    g.add_node("responder",    _make_responder_node(llm))
    g.add_node("chat",         _make_chat_node(llm))
    g.add_node("rule_builder", _make_rule_builder_node())
    g.add_node("discovery",    _make_discovery_node())

    g.set_entry_point("planner")
    g.add_conditional_edges(
        "planner",
        lambda s: s.get("route", "act"),
        {
            "act": "dispatcher",
            "chat": "chat",
            "define_rule": "rule_builder",
            "discover_devices": "discovery",
        },
    )
    g.add_edge("dispatcher",   "evaluator")
    g.add_edge("evaluator",    "responder")
    g.add_edge("responder",    END)
    g.add_edge("chat",         END)
    g.add_edge("rule_builder", END)
    g.add_edge("discovery",    END)

    return g.compile()
