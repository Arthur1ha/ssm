# graph.py — 多智能体编排图
# Planner → Dispatcher → Evaluator → Responder
# 支持 ESP32（MQTT）和 Go2（HTTP）两种传输协议

import os
import re
import json
import time as _time
from typing import TypedDict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END

import tools as _t

_API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8082")


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

        all_devices = _t._state.get_all_devices()
        if not all_devices:
            _t.do_publish_feedback(session_id, "failed",
                "抱歉，我还没有发现任何在线设备，请确认设备已连接。")
            return {**state, "planned_tasks": [], "early_exit": True}

        lines = []
        for uid, m in all_devices.items():
            hw = m.get("hw_platform", "unknown")
            caps = json.dumps(m.get("capabilities", []), ensure_ascii=False)
            tags = m.get("resource_tags", [])
            tag_str = f"  标签: {', '.join(tags)}" if tags else ""
            lines.append(f"- {uid}（平台: {hw}）能力: {caps}{tag_str}")
        device_str = "\n".join(lines)

        prompt = (
            f"你是 SSM 多智能体控制中枢。根据用户意图，为合适的设备生成控制任务。\n\n"
            f"用户原话：{state['user_msg']}\n"
            f"意图解析：{json.dumps(state['requirements'], ensure_ascii=False)}\n\n"
            f"当前在线设备：\n{device_str}\n\n"
            f"路由规则：\n"
            f"1. hw_platform=esp32 的设备 → 使用 MQTT 动作，action 只能是：\n"
            f"   SET_STATE（params: {{state: BRIGHT|DIM|OFF}}）\n"
            f"   SET_COLOR（params: {{r,g,b,brightness: 0-255}}）\n"
            f"   PLAY（params: {{pattern: NOTIFY|ALERT}}）\n"
            f"   每条 requirement 严格一个任务，禁止同一设备生成多个串行任务\n"
            f"2. hw_platform=go2 的设备 → 使用 action=CHAT，\n"
            f"   params: {{\"message\": \"简洁中文指令\"}}，由 Go2 智能体进一步解析\n\n"
            f"输出 JSON 数组，不含代码块，每项包含 device_id、action、params。\n"
            f"示例：\n"
            f'  [{{"device_id": "esp32_desk_led", "action": "SET_STATE", "params": {{"state": "BRIGHT"}}}},\n'
            f'   {{"device_id": "go2", "action": "CHAT", "params": {{"message": "请坐下"}}}}]'
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

        pre_results = {}
        for task in tasks:
            manifest  = _t._state.get_manifest(task["device_id"])
            hw        = (manifest or {}).get("hw_platform", "esp32")

            if hw == "go2":
                # HTTP 委托到 Go2 智能体，同步等待响应
                instruction = task["params"].get("message", state["user_msg"])
                result = _t.do_dispatch_http(
                    f"{_API_BASE}/api/go2/chat",
                    {"session_id": session_id, "message": instruction},
                )
                status = "ok" if "error" not in result else "error"
                pre_results[task["task_id"]] = {"result": status, **result}
                print(f"[Dispatcher] go2 HTTP → {instruction!r} → {status}")
            else:
                # MQTT 派发到 ESP32
                _t.do_publish_task(
                    task["device_id"], task["task_id"],
                    task["action"], task["params"], session_id,
                )
                print(f"[Dispatcher] mqtt → {task['device_id']} {task['action']} task_id={task['task_id']}")

        return {**state, "task_results": pre_results}

    # ── Evaluator ─────────────────────────────────────────────
    def evaluator_node(state: OrchestratorState) -> OrchestratorState:
        if state.get("early_exit"):
            return state
        tasks = state["planned_tasks"]
        if not tasks:
            return state

        results = dict(state.get("task_results", {}))  # 已含 go2 同步结果

        # 轮询等待 MQTT 结果（ESP32 任务）
        mqtt_tasks = [t for t in tasks if t["task_id"] not in results]
        if mqtt_tasks:
            deadline = _time.time() + 5.0
            while _time.time() < deadline:
                for task in list(mqtt_tasks):
                    r = _t._state.get_task_result(task["task_id"])
                    if r:
                        results[task["task_id"]] = r
                        mqtt_tasks.remove(task)
                if not mqtt_tasks:
                    break
                _time.sleep(0.2)

            for task in mqtt_tasks:
                results[task["task_id"]] = {"result": "timeout", "task_id": task["task_id"]}

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

        fallbacks = {
            "done":    "好的，指令已全部执行！",
            "partial": "部分指令已执行。",
            "failed":  "设备未响应，请检查连接后重试。",
        }
        try:
            if stage == "done":
                prompt = (f"用户说：'{state['user_msg']}'。设备已全部执行成功。"
                          f"用1句简短中文告诉用户结果，语气自然友好，不提技术细节。")
            elif stage == "partial":
                prompt = (f"用户说：'{state['user_msg']}'。部分设备成功（{statuses}）。"
                          f"用1句简短中文说明并给出建议。")
            else:
                tried   = [f"{t['action']}({t['params']})" for t in state.get("planned_tasks", [])]
                reasons = {k: v.get("result") for k, v in results.items()}
                prompt  = (f"用户说：'{state['user_msg']}'。"
                           f"系统尝试{tried}但失败（{reasons}）。"
                           f"用1句简短友好中文告诉用户失败，建议检查设备连接，不提技术细节。")
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
