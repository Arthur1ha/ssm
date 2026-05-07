# graph.py — LangGraph graphs
#
# build_graph()        : V1 sensor-triggered Decision + Evaluation agents
# build_orchestrator() : V2 user-intent Planner→Dispatcher→Evaluator→Responder

import os
import re
import json
import time as _time
from typing import TypedDict, Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent
from langgraph.graph import StateGraph, END

import tools as _t
from tools import (
    get_sensor_snapshot, get_capabilities,
    publish_led_command, publish_buzzer_command,
    get_last_decision, get_actuator_snapshot, publish_assessment,
)

# ── Shared LLM factory（带 fallback 链）────────────────────────

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


# ════════════════════════════════════════════════════════════
#  V1 — Sensor-triggered graph (unchanged)
# ════════════════════════════════════════════════════════════

DECISION_PROMPT = """你是 SSM 智能家居决策智能体。收到传感器事件后，根据当前环境和可用设备能力自主决策。

工作流程：
1. 调用 get_sensor_snapshot 了解所有传感器当前读数
2. 调用 get_capabilities 查看可用设备及其支持的操作
3. 综合分析：当前环境需要何种响应？哪些设备可以响应？
4. 有必要时调用对应工具发出控制指令；确实不需要响应时不调用任何控制工具

决策原则：
- 以用户体验和实际场景为判断依据，不机械套规则
- 只在有明确必要性时才控制设备
- 一次事件发出最简洁的指令，避免过度响应"""

EVALUATION_PROMPT = """你是 SSM 执行效果评估智能体，检验决策智能体的指令是否被设备正确执行。

工作流程：
1. 调用 get_last_decision 获取上一次决策智能体下发的指令
2. 调用 get_actuator_snapshot 获取执行器的实际反馈
3. 比对意图与结果：
   - result="ok" 且状态符合预期 → 评估为 "ok"
   - result="blocked"（状态机拒绝）→ 评估为 "blocked"，说明原因
   - 指令与执行不一致 → 评估为 "mismatch"
4. 调用 publish_assessment 发布评估结论（简洁中文）"""


class RouterState(TypedDict):
    trigger: Literal["sensor", "actuator"]
    payload: dict


def build_graph():
    llm = _make_llm()

    decision_react = create_react_agent(
        llm,
        tools=[get_sensor_snapshot, get_capabilities,
               publish_led_command, publish_buzzer_command],
        prompt=DECISION_PROMPT,
    )
    evaluation_react = create_react_agent(
        llm,
        tools=[get_last_decision, get_actuator_snapshot, publish_assessment],
        prompt=EVALUATION_PROMPT,
    )

    def _stream(tag, agent, messages):
        for chunk in agent.stream(messages, stream_mode="updates"):
            for node_name, node_data in chunk.items():
                for msg in node_data.get("messages", []):
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            print(f"  [{tag}] 调用工具 → {tc['name']}({tc.get('args', {})})")
                    elif hasattr(msg, "content") and msg.content:
                        print(f"  [{tag}/{type(msg).__name__}] {msg.content}")

    def decision_node(state: RouterState) -> RouterState:
        snapshot = get_sensor_snapshot.invoke({})
        prompt = (f"传感器当前快照：{snapshot}\n"
                  f"本次触发报告：{state['payload']}\n\n"
                  f"请立即调用 publish_led_command 发出控制指令。")
        print("  [Decision] 开始推理...")
        _stream("Decision", decision_react, {"messages": [HumanMessage(content=prompt)]})
        return state

    def evaluation_node(state: RouterState) -> RouterState:
        prompt = f"收到执行器反馈，payload={state['payload']}。请评估执行效果。"
        print("  [Evaluation] 开始评估...")
        _stream("Evaluation", evaluation_react, {"messages": [HumanMessage(content=prompt)]})
        return state

    def router_node(state: RouterState) -> RouterState:
        return state

    def route(state: RouterState) -> Literal["decision_node", "evaluation_node"]:
        return "decision_node" if state["trigger"] == "sensor" else "evaluation_node"

    g = StateGraph(RouterState)
    g.add_node("router",          router_node)
    g.add_node("decision_node",   decision_node)
    g.add_node("evaluation_node", evaluation_node)
    g.set_entry_point("router")
    g.add_conditional_edges("router", route, {
        "decision_node":   "decision_node",
        "evaluation_node": "evaluation_node",
    })
    g.add_edge("decision_node",   END)
    g.add_edge("evaluation_node", END)
    return g.compile()


# ════════════════════════════════════════════════════════════
#  V2 — Intent-driven Orchestrator
# ════════════════════════════════════════════════════════════

class OrchestratorState(TypedDict):
    session_id:   str
    user_msg:     str
    requirements: list
    planned_tasks: list   # [{device_id, task_id, action, params}]
    task_results:  dict   # task_id → result payload
    response_text: str
    early_exit:    bool   # True = feedback already sent, skip remaining nodes


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

        # Build device capability summary — deduplicate by uid, merge all tags
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
            f"2. 将 action 映射为设备支持的操作：\n"
            f"   brighten/on → SET_STATE state=BRIGHT\n"
            f"   dim → SET_STATE state=DIM\n"
            f"   off → SET_STATE state=OFF\n"
            f"   set_color → SET_COLOR（选择合适的 r/g/b/brightness）\n"
            f"   notify → PLAY pattern=NOTIFY\n"
            f"   alert → PLAY pattern=ALERT\n"
            f"3. 用户描述的是场景（如读书、睡眠、休息），根据场景选择最合适的灯光参数\n\n"
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

        deadline = _time.time() + 2.0
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

        # Fill timed-out tasks
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
                reasons = {k: v.get("result") for k, v in results.items()}
                prompt = (f"用户说：'{state['user_msg']}'。设备未能执行（{reasons}）。"
                          f"用1句简短友好中文解释并给出替代建议，不提技术细节。")
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
