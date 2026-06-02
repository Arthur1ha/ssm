import asyncio
import json
import os
import re
import sys as _sys
import time
from pathlib import Path
from typing import TypedDict

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from cloud.go2.connection import go2

RULES_FILE = Path(__file__).parent / "rules.json"


def load_rules() -> list:
    if not RULES_FILE.exists():
        return []
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def save_rules(rules: list) -> None:
    RULES_FILE.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def check_rules(observation: str) -> list[str]:
    rules = load_rules()
    now = time.time()
    triggered = []
    changed = False
    for rule in rules:
        if rule["trigger"] in observation:
            if now - rule.get("last_triggered", 0) >= rule["cooldown_s"]:
                rule["last_triggered"] = now
                triggered.append(rule["action"])
                changed = True
    if changed:
        save_rules(rules)
    return triggered


# ── LLM 单例 ─────────────────────────────────────────────────────

_text_llm = None
_vision_llm = None


def get_text_llm() -> ChatOpenAI:
    global _text_llm
    if _text_llm is None:
        _text_llm = ChatOpenAI(
            model=os.getenv("GO2_TEXT_MODEL", "deepseek-chat"),
            base_url=os.getenv("SJTU_BASE_URL"),
            api_key=os.getenv("SJTU_API_KEY"),
            temperature=0,
            timeout=30,
        )
    return _text_llm


def get_vision_llm() -> ChatOpenAI:
    global _vision_llm
    if _vision_llm is None:
        _vision_llm = ChatOpenAI(
            model=os.getenv("GO2_VISION_MODEL", "qwen"),
            base_url=os.getenv("SJTU_BASE_URL"),
            api_key=os.getenv("SJTU_API_KEY"),
            temperature=0,
            timeout=30,
        )
    return _vision_llm


# ── 工具函数 ──────────────────────────────────────────────────────

_VALID_SPORT_CMDS = {"StandUp", "StandDown", "StopMove", "Hello", "Stretch", "Dance1", "Dance2"}

_DIRECTION_MAP = {
    "forward":    lambda s: {"x":  s, "y":  0, "z":  0},
    "backward":   lambda s: {"x": -s, "y":  0, "z":  0},
    "left":       lambda s: {"x":  0, "y":  s, "z":  0},
    "right":      lambda s: {"x":  0, "y": -s, "z":  0},
    "turn_left":  lambda s: {"x":  0, "y":  0, "z":  s},
    "turn_right": lambda s: {"x":  0, "y":  0, "z": -s},
}


async def go2_sport(cmd: str) -> str:
    if not go2.is_connected:
        return "Go2 未连接，请先建立连接"
    if cmd not in _VALID_SPORT_CMDS:
        return f"未知动作 {cmd}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    await go2.send_command(cmd)
    return f"已执行 {cmd}"


async def go2_move(direction: str, speed: float = 0.3, duration: float = 1.0) -> str:
    if not go2.is_connected:
        return "Go2 未连接，请先建立连接"
    if direction not in _DIRECTION_MAP:
        return f"未知方向 {direction}，支持: {', '.join(_DIRECTION_MAP)}"
    params = _DIRECTION_MAP[direction](speed)
    await go2.send_command("Move", params)
    await asyncio.sleep(duration)
    await go2.send_command("StopMove")
    return f"已向 {direction} 移动 {duration}s"


async def go2_observe(question: str = "描述你看到的场景") -> str:
    frame_b64 = go2.latest_frame_b64()
    if not frame_b64:
        return "无可用视频帧，请确认 Go2 已连接且视频流正常"
    resp = await get_vision_llm().ainvoke([HumanMessage(content=[
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
    ])])
    return resp.content


def go2_status() -> str:
    if not go2.is_connected:
        return "Go2 未连接"
    s = go2._robot_state
    return f"已连接 | mode={s.get('mode')} body_height={s.get('body_height')} velocity={s.get('velocity')}"


def go2_add_rule(trigger: str, action: str, cooldown_s: int = 30) -> str:
    if action not in _VALID_SPORT_CMDS:
        return f"不支持的动作 {action}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    rules = load_rules()
    rules = [r for r in rules if not (r["trigger"] == trigger and r["action"] == action)]
    rules.append({"trigger": trigger, "action": action,
                  "cooldown_s": cooldown_s, "last_triggered": 0})
    save_rules(rules)
    return f"已添加规则：检测到「{trigger}」时执行 {action}，冷却 {cooldown_s}s"


def go2_list_rules() -> str:
    rules = load_rules()
    if not rules:
        return "当前没有规则"
    return "\n".join(
        f"- 检测到「{r['trigger']}」→ {r['action']}（冷却 {r['cooldown_s']}s）"
        for r in rules
    )


# ── State ─────────────────────────────────────────────────────────

class Go2AgentState(TypedDict):
    session_id:    str
    user_msg:      str
    planned_tools: list
    tool_results:  list
    response_text: str
    early_exit:    bool


# ── 工具分发表 ────────────────────────────────────────────────────

_TOOL_FN_MAP = {
    "go2_sport":      go2_sport,
    "go2_move":       go2_move,
    "go2_observe":    go2_observe,
    "go2_status":     go2_status,
    "go2_add_rule":   go2_add_rule,
    "go2_list_rules": go2_list_rules,
}

_TOOL_DESCRIPTIONS = """\
可用工具：
- go2_sport(cmd): 执行预定义动作。cmd 取值：StandUp/StandDown/StopMove/Hello/Stretch/Dance1/Dance2
- go2_move(direction, speed=0.3, duration=1.0): 移动机器狗。direction: forward/backward/left/right/turn_left/turn_right
- go2_observe(question="描述你看到的场景"): 用摄像头分析当前画面，返回视觉描述
- go2_status(): 查询连接状态和机器狗当前姿态
- go2_add_rule(trigger, action, cooldown_s=30): 添加视觉触发规则，检测到 trigger 关键词时自动执行 action
- go2_list_rules(): 列出当前所有视觉触发规则"""


# ── Planner 节点 ──────────────────────────────────────────────────

async def planner_node(state: Go2AgentState) -> Go2AgentState:
    if not go2.is_connected:
        return {**state, "planned_tools": [], "early_exit": True,
                "response_text": "Go2 当前未连接，请先通过「连接」按钮建立连接。"}

    prompt = (
        f"你是 Go2 机器狗控制智能体。根据用户指令生成工具调用列表。\n"
        f"{_TOOL_DESCRIPTIONS}\n\n"
        f"用户指令：{state['user_msg']}\n\n"
        f"规则：\n"
        f"1. 涉及视觉问题（看到什么/有没有人/描述场景）必须调用 go2_observe\n"
        f"2. 最多生成 3 个工具调用\n"
        f"3. 直接输出 JSON 数组，不含解释或代码块\n"
        f"示例：[{{\"tool\": \"go2_sport\", \"params\": {{\"cmd\": \"StandUp\"}}}}]"
    )
    resp = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
    content = resp.content.strip()
    content = re.sub(r"```(?:json)?\n?", "", content).strip().rstrip("`").strip()
    idx_s, idx_e = content.find("["), content.rfind("]")
    try:
        tools_raw = json.loads(content[idx_s:idx_e + 1]) if idx_s != -1 else []
    except Exception:
        tools_raw = []
    planned = [t for t in tools_raw if isinstance(t, dict) and "tool" in t]
    return {**state, "planned_tools": planned, "early_exit": False}


# ── Executor 节点 ─────────────────────────────────────────────────

async def executor_node(state: Go2AgentState) -> Go2AgentState:
    if state.get("early_exit"):
        return state

    results = []
    for call in state["planned_tools"]:
        tool_name = call.get("tool", "")
        params = call.get("params", {})
        fn = _TOOL_FN_MAP.get(tool_name)
        if fn is None:
            results.append({"tool": tool_name, "result": f"未知工具: {tool_name}"})
            continue
        try:
            result = await fn(**params) if asyncio.iscoroutinefunction(fn) else fn(**params)
        except Exception as exc:
            result = f"执行失败: {exc}"
        results.append({"tool": tool_name, "result": result})

        if tool_name == "go2_observe":
            for action in check_rules(result):
                try:
                    await go2_sport(action)
                    results.append({"tool": "rule_trigger", "result": f"规则触发: {action}"})
                except Exception as exc:
                    results.append({"tool": "rule_trigger", "result": f"规则触发失败: {exc}"})

    results_text = "\n".join(f"- {r['tool']}: {r['result']}" for r in results)
    prompt = (
        f"用户说：'{state['user_msg']}'\n"
        f"执行结果：\n{results_text}\n"
        f"用 1 句简短中文告诉用户结果，语气自然，不提技术细节。"
    )
    try:
        resp = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
        response_text = resp.content.strip()
    except Exception:
        response_text = "指令已执行完成。"

    return {**state, "tool_results": results, "response_text": response_text}


# ── 图组装 + 入口 ─────────────────────────────────────────────────


def _build_agent():
    # 用间接调用确保测试中 monkeypatch 替换模块属性后仍能生效
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


async def run_agent(session_id: str, message: str) -> dict:
    state = await _get_agent().ainvoke({
        "session_id":    session_id,
        "user_msg":      message,
        "planned_tools": [],
        "tool_results":  [],
        "response_text": "",
        "early_exit":    False,
    })
    return {
        "response":      state["response_text"],
        "actions_taken": [r["tool"] for r in state["tool_results"]],
    }
