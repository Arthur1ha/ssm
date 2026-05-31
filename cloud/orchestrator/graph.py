# graph.py — 用户意图编排图
# Planner → Dispatcher → Evaluator → Responder

import os
import re
import json
import time as _time
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END

import tools as _t


def _make_llm():
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
    session_id:    str
    user_msg:      str
    requirements:  list
    planned_tasks: list   # [{device_id, task_id, action, params}]
    task_results:  dict   # task_id → result payload
    response_text: str
    early_exit:    bool   # True = feedback 已发出，跳过后续节点


def build_orchestrator():
    llm = _make_llm()

    # ── Planner ───────────────────────────────────────────────
    def planner_node(state: OrchestratorState) -> OrchestratorState:
        session_id = state["session_id"]
        _t.do_publish_feedback(session_id, "planning", "正在规划控制方案...")

        registry = _t._state.get_capability_registry()
        if not registry:
            _t.do_publish_feedback(session_id, "failed",
                "抱歉，我还没有发现附近的设备，请确认 ESP32 已上线。")
            return {**state, "planned_tasks": [], "early_exit": True}

        uid_tags = {}
        for tag, device_ids in registry.items():
            for uid in device_ids:
                uid_tags.setdefault(uid, []).append(tag)

        lines = []
        for uid, tags in uid_tags.items():
            m = _t._state.get_manifest(uid)
            if m:
                caps = json.dumps(m.get("capabilities", []), ensure_ascii=False)
                lines.append(f"设备 {uid}（标签: {', '.join(tags)}）能力: {caps}")
        device_str = "\n".join(lines) if lines else "无可用设备"

        prompt = (
            f"你是 SSM 智能家居控制中枢。根据用户意图生成设备控制任务列表。\n\n"
            f"用户原话：{state['user_msg']}\n"
            f"意图解析：{json.dumps(state['requirements'], ensure_ascii=False)}\n\n"
            f"可用设备：\n{device_str}\n\n"
            f"规则：\n"
            f"1. 将 requirements 中的 resource_tag 与设备标签匹配\n"
            f"2. 将 action 直接映射为设备操作（严格一对一，每条 requirement 只生成一个任务）：\n"
            f"   brighten/on → SET_STATE state=BRIGHT\n"
            f"   dim → SET_STATE state=DIM（禁止先生成 OFF 再 DIM，直接 DIM）\n"
            f"   off → SET_STATE state=OFF\n"
            f"   set_color → SET_COLOR（选择合适的 r/g/b/brightness）\n"
            f"   notify → PLAY pattern=NOTIFY\n"
            f"   alert → PLAY pattern=ALERT\n"
            f"3. 用户描述场景（如读书、睡眠）时，选择合适的 SET_COLOR 参数\n"
            f"4. 严格限制：每条 requirement 只生成一个任务，禁止为同一设备生成多个串行任务\n\n"
            f"直接输出 JSON 数组，不含代码块或解释，每项包含 device_id、action、params。\n"
            f"示例：[{{\"device_id\": \"esp32_desk_led\", \"action\": \"SET_STATE\", \"params\": {{\"state\": \"BRIGHT\"}}}}]"
        )

        try:
            resp = llm.invoke([HumanMessage(content=prompt)])
            content = resp.content.strip()
            content = re.sub(r'```(?:json)?\n?', '', content).strip().rstrip('`').strip()
            idx_s = content.find('[')
            idx_e = content.rfind(']')
            tasks_raw = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else []
        except Exception as e:
            print(f"[Planner] parse error: {e}")
            tasks_raw = []

        tasks = [
            {
                "device_id": t["device_id"],
                "task_id":   f"{session_id}_t{i}",
                "action":    t["action"],
                "params":    t.get("params", {}),
            }
            for i, t in enumerate(tasks_raw)
            if isinstance(t, dict) and "device_id" in t and "action" in t
        ]
        print(f"[Planner] session={session_id} planned {len(tasks)} task(s)")
        return {**state, "planned_tasks": tasks, "early_exit": False}

    # ── Dispatcher ────────────────────────────────────────────
    def dispatcher_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        session_id = state["session_id"]
        tasks = state["planned_tasks"]

        if not tasks:
            _t.do_publish_feedback(session_id, "failed",
                "抱歉，我没找到合适的设备来完成这个请求。")
            return {**state, "early_exit": True}

        _t.do_publish_feedback(session_id, "executing", "正在控制设备...")
        for task in tasks:
            _t.do_publish_task(
                task["device_id"], task["task_id"],
                task["action"], task["params"], session_id,
            )
            print(f"[Dispatcher] → {task['device_id']} {task['action']} task_id={task['task_id']}")
        return state

    # ── Evaluator ─────────────────────────────────────────────
    def evaluator_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        tasks = state["planned_tasks"]
        if not tasks:
            return state

        deadline = _time.time() + 5.0
        results = {}
        while _time.time() < deadline:
            for task in tasks:
                tid = task["task_id"]
                if tid not in results:
                    r = _t._state.get_task_result(tid)
                    if r:
                        results[tid] = r
            if len(results) == len(tasks):
                break
            _time.sleep(0.2)

        for task in tasks:
            tid = task["task_id"]
            if tid not in results:
                results[tid] = {"result": "timeout", "task_id": tid}

        print(f"[Evaluator] results: {results}")
        return {**state, "task_results": results}

    # ── Responder ─────────────────────────────────────────────
    def responder_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        session_id = state["session_id"]
        results = state["task_results"]
        if not results:
            return state

        statuses  = [r.get("result", "timeout") for r in results.values()]
        ok_count  = statuses.count("ok")
        stage     = "done" if ok_count == len(results) else ("partial" if ok_count > 0 else "failed")

        fallbacks = {"done": "好的，指令已全部执行！", "partial": "部分指令已执行。",
                     "failed": "设备未响应，请检查连接后重试。"}
        try:
            if stage == "done":
                prompt = (f"用户说：'{state['user_msg']}'。设备已全部执行成功。"
                          f"用1句简短中文告诉用户结果，语气自然友好，不提技术细节。")
            elif stage == "partial":
                prompt = (f"用户说：'{state['user_msg']}'。部分设备成功（{statuses}）。"
                          f"用1句简短中文说明并给出建议。")
            else:
                tried = [f"{t['action']}({t['params']})" for t in state.get("planned_tasks", [])]
                reasons = {k: v.get("result") for k, v in results.items()}
                prompt = (f"用户说：'{state['user_msg']}'。"
                          f"系统尝试对设备执行{tried}但失败（{reasons}）。"
                          f"用1句简短友好中文告诉用户设备控制失败，建议检查设备连接，不提技术细节。")
            resp = llm.invoke([HumanMessage(content=prompt)])
            text = resp.content.strip()
        except Exception:
            text = fallbacks[stage]

        _t.do_publish_feedback(session_id, stage, text)
        print(f"[Responder] session={session_id} stage={stage} → {text}")
        return {**state, "response_text": text}

    # ── Assemble ──────────────────────────────────────────────
    g = StateGraph(OrchestratorState)
    g.add_node("planner",    planner_node)
    g.add_node("dispatcher", dispatcher_node)
    g.add_node("evaluator",  evaluator_node)
    g.add_node("responder",  responder_node)

    g.set_entry_point("planner")
    g.add_edge("planner",    "dispatcher")
    g.add_edge("dispatcher", "evaluator")
    g.add_edge("evaluator",  "responder")
    g.add_edge("responder",  END)

    return g.compile()
