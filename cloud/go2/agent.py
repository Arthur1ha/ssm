import asyncio
import json
import os
import time
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

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
