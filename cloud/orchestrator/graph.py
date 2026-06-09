"""graph.py — 用户意图编排图（card-driven）。

Planner → Dispatcher → Evaluator → Responder 四节点有向图。
Planner 读 CardRegistry 的 card skills 让 LLM 自由规划（无硬编码路由规则）；
Dispatcher 按 card.transport.kind 分支：mqtt 走 MQTT 发布，http 走线程池非阻塞；
Evaluator 只对 mqtt task 轮询 result topic，http 结果由 Dispatcher 已填好。
"""

import os
import re
import json
import time as _time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, wait
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END

import tools as _t


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
    print(f"[Graph] LLM fallback chain: {' → '.join(models)}")
    return llms[0].with_fallbacks(llms[1:]) if len(llms) > 1 else llms[0]


class OrchestratorState(TypedDict):
    """编排图共享状态。

    requirements 为 NLU 结果，可选（Task 5 退役 NLU 后将为空）。
    planned_tasks 每项形如 {slug, skill_id, task_id, params}。
    """

    session_id:    str
    user_msg:      str
    requirements:  list   # NLU 结果，可选，默认 []
    planned_tasks: list   # [{slug, skill_id, task_id, params}]
    task_results:  dict   # task_id → result payload
    response_text: str
    early_exit:    bool    # True = feedback 已发出，跳过后续节点


# ── prompt 构建 ──────────────────────────────────────────────────

def _build_card_prompt(cards: dict, user_msg: str, requirements: list) -> str:
    """把所有在线 card 的 skills + params_schema 注入 prompt，无硬编码路由规则。"""
    lines = []
    for slug, card in cards.items():
        if not card.get("online", True):
            continue
        lines.append(f"- {slug}（{card.get('name', slug)}）：")
        for skill in card.get("skills", []):
            schema = skill.get("params_schema", {})
            props = schema.get("properties", {})
            params_desc = json.dumps(props, ensure_ascii=False)
            lines.append(f"  skill: {skill['id']}（{skill.get('name', '')}）params: {params_desc}")
    agents_str = "\n".join(lines) if lines else "无可用智能体"

    return (
        f"你是 SSM 多智能体编排中枢。根据用户意图，从可用智能体中挑选合适的 skill 生成任务列表。\n\n"
        f"用户原话：{user_msg}\n"
        f"意图解析（可能为空）：{json.dumps(requirements, ensure_ascii=False)}\n\n"
        f"可用智能体：\n{agents_str}\n\n"
        f"要求：\n"
        f"1. 仅使用上面列出的 slug 和 skill_id，params 必须符合对应 skill 的 params_schema。\n"
        f"2. 每个意图选择最贴切的一个 skill，禁止为同一目标生成多个串行任务。\n"
        f"3. 描述场景（如读书、睡眠）时，自行选择合适的参数值。\n"
        f"4. 找不到合适的智能体时输出空数组 []。\n\n"
        f"直接输出 JSON 数组，不含代码块或解释，每项包含 slug、skill_id、params。\n"
        f'示例：[{{"slug": "esp32_desk_led", "skill_id": "set_light_state", "params": {{"state": "BRIGHT"}}}}]'
    )


def _parse_tasks(content: str) -> list:
    """从 LLM 输出中提取 JSON 数组，剥离代码块包裹。"""
    content = content.strip()
    content = re.sub(r'```(?:json)?\n?', '', content).strip().rstrip('`').strip()
    idx_s = content.find('[')
    idx_e = content.rfind(']')
    if idx_s == -1 or idx_e == -1:
        return []
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
    """构建 Planner 节点：读 CardRegistry，LLM 自由规划，校验后产出任务。"""

    def planner_node(state: OrchestratorState) -> OrchestratorState:
        session_id = state["session_id"]
        _t.do_publish_feedback(session_id, "planning", "正在规划控制方案...")

        cards = _t._registry.get_all_cards() if _t._registry else {}
        if not cards:
            _t.do_publish_feedback(session_id, "failed",
                "抱歉，我还没有发现可用的智能体，请确认设备已上线。")
            return {**state, "planned_tasks": [], "early_exit": True}

        prompt = _build_card_prompt(cards, state["user_msg"], state.get("requirements", []))

        try:
            resp = llm.invoke([HumanMessage(content=prompt)])
            tasks_raw = _parse_tasks(resp.content)
        except Exception as e:
            print(f"[Planner] parse error: {e}")
            tasks_raw = []

        tasks = []
        for i, t in enumerate(tasks_raw):
            if not isinstance(t, dict):
                continue
            slug = t.get("slug")
            skill_id = t.get("skill_id")
            card = cards.get(slug)
            if not card:
                print(f"[Planner] 丢弃未知 slug: {slug}")
                continue
            if not any(s.get("id") == skill_id for s in card.get("skills", [])):
                print(f"[Planner] 丢弃 {slug} 未知 skill_id: {skill_id}")
                continue
            tasks.append({
                "slug":     slug,
                "skill_id": skill_id,
                "task_id":  f"{session_id}_t{i}",
                "params":   t.get("params", {}),
            })

        print(f"[Planner] session={session_id} planned {len(tasks)} task(s)")
        return {**state, "planned_tasks": tasks, "early_exit": False}

    return planner_node


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
            slug = task["slug"]
            card = _t._registry.get_card(slug) if _t._registry else None
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
                _t.do_publish_task(slug, task["task_id"], action, task["params"], session_id)
                print(f"[Dispatcher] mqtt → {slug} {action} task_id={task['task_id']}")

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
                print(f"[Dispatcher] http → {slug} {endpoint} task_id={task['task_id']} timeout={timeout}s")

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
                    print(f"[Dispatcher] http task {task_id} timed out")
                    results[task_id] = {"result": "timeout", "task_id": task_id}
                except Exception as e:
                    print(f"[Dispatcher] http task {task_id} error: {e}")
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
            slug = task["slug"]
            card = _t._registry.get_card(slug) if _t._registry else None
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

        print(f"[Evaluator] results: {results}")
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
                tried = [f"{t['slug']}.{t['skill_id']}({t['params']})" for t in state.get("planned_tasks", [])]
                reasons = {k: v.get("result") for k, v in results.items()}
                prompt = (f"用户说：'{state['user_msg']}'。"
                          f"系统尝试执行{tried}但失败（{reasons}）。"
                          f"用1句简短友好中文告诉用户执行失败，建议检查设备连接，不提技术细节。")
            resp = llm.invoke([HumanMessage(content=prompt)])
            text = resp.content.strip()
        except Exception:
            text = fallbacks[stage]

        _t.do_publish_feedback(session_id, stage, text)
        print(f"[Responder] session={session_id} stage={stage} → {text}")
        return {**state, "response_text": text}

    return responder_node


def build_orchestrator():
    """装配 Planner→Dispatcher→Evaluator→Responder 编排图。"""
    llm = _make_llm()

    g = StateGraph(OrchestratorState)
    g.add_node("planner",    _make_planner_node(llm))
    g.add_node("dispatcher", _make_dispatcher_node())
    g.add_node("evaluator",  _make_evaluator_node())
    g.add_node("responder",  _make_responder_node(llm))

    g.set_entry_point("planner")
    g.add_edge("planner",    "dispatcher")
    g.add_edge("dispatcher", "evaluator")
    g.add_edge("evaluator",  "responder")
    g.add_edge("responder",  END)

    return g.compile()
