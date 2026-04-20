# graph.py — LangGraph multi-agent definition.
#
# Two ReAct agents connected by a router:
#
#   sensor report  → Decision Agent  → publish_led_command / publish_buzzer_command
#   actuator report→ Evaluation Agent→ publish_assessment
#
#        [START]
#           │
#       [router]  ──sensor──▶  [decision_node]  ──▶ [END]
#                 ──actuator─▶ [evaluation_node] ──▶ [END]

import os
from typing import TypedDict, Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent
from langgraph.graph import StateGraph, END

from tools import (
    get_sensor_snapshot, publish_led_command, publish_buzzer_command,
    get_last_decision, get_actuator_snapshot, publish_assessment,
)

# ── System prompts ────────────────────────────────────────────

DECISION_PROMPT = """你是一个智能照明决策智能体。传感器数据已在消息中提供。

立即调用 publish_led_command 工具，规则如下：
- 光线为 DARK 或 DIM → cmd="SET_COLOR", r=255, g=160, b=60, brightness=180
- 光线为 BRIGHT → cmd="SET_STATE", state="OFF"
- 检测到声音 → cmd="BLINK", r=255, g=255, b=255, count=2

只调用工具，不要输出解释文字。"""

EVALUATION_PROMPT = """你是一个执行效果评估智能体，负责检验决策智能体的命令是否被 ESP32 正确执行。

你的工作流程：
1. 调用 get_last_decision 获取上一次决策智能体下发的命令
2. 调用 get_actuator_snapshot 获取执行器的实际反馈（report 字段包含 cmd、result、ism_state）
3. 比对：
   - result="ok" 且 ism_state 与预期一致 → 评估为 "ok"
   - result="blocked"（ISM 拒绝了状态转移）→ 评估为 "blocked"，说明原因
   - 命令与实际执行的不一致 → 评估为 "mismatch"
4. 调用 publish_assessment 发布评估结论

用简洁的中文说明评估结果。"""


# ── Graph state ───────────────────────────────────────────────

class RouterState(TypedDict):
    trigger: Literal["sensor", "actuator"]
    payload: dict


# ── Build graph ───────────────────────────────────────────────

def build_graph():
    key = os.getenv("OPENAI_API_KEY", "")
    print(f"[Graph] Using API key: {key[:12]}...  base_url: {os.getenv('OPENAI_BASE_URL')}")
    llm = ChatOpenAI(
        model=os.getenv("MODEL"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0,
        timeout=30,
    )

    decision_react = create_react_agent(
        llm,
        tools=[get_sensor_snapshot, publish_led_command, publish_buzzer_command],
        prompt=DECISION_PROMPT,
    )

    evaluation_react = create_react_agent(
        llm,
        tools=[get_last_decision, get_actuator_snapshot, publish_assessment],
        prompt=EVALUATION_PROMPT,
    )

    # ── Nodes ─────────────────────────────────────────────────

    def _stream_agent(tag: str, agent, messages: dict):
        for chunk in agent.stream(messages, stream_mode="updates"):
            for node_name, node_data in chunk.items():
                for msg in node_data.get("messages", []):
                    cls = type(msg).__name__
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        for tc in msg.tool_calls:
                            print(f"  [{tag}] 调用工具 → {tc['name']}({tc.get('args', {})})")
                    elif hasattr(msg, "content") and msg.content:
                        print(f"  [{tag}/{cls}] {msg.content}")

    def decision_node(state: RouterState) -> RouterState:
        snapshot = get_sensor_snapshot.invoke({})
        prompt = (
            f"传感器当前快照：{snapshot}\n"
            f"本次触发报告：{state['payload']}\n\n"
            f"请立即调用 publish_led_command 发出控制指令。"
        )
        print(f"  [Decision] 开始推理...")
        _stream_agent("Decision", decision_react, {"messages": [HumanMessage(content=prompt)]})
        return state

    def evaluation_node(state: RouterState) -> RouterState:
        prompt = f"收到执行器反馈，payload={state['payload']}。请评估执行效果。"
        print(f"  [Evaluation] 开始评估...")
        _stream_agent("Evaluation", evaluation_react, {"messages": [HumanMessage(content=prompt)]})
        return state

    def router_node(state: RouterState) -> RouterState:
        return state

    def route(state: RouterState) -> Literal["decision_node", "evaluation_node"]:
        return "decision_node" if state["trigger"] == "sensor" else "evaluation_node"

    # ── Assemble ──────────────────────────────────────────────

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
