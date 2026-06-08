import asyncio
import json
import os
import time
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from cloud.go2.connection import go2

# ── 规则存储 ──────────────────────────────────────────────────────

RULES_FILE = Path(__file__).parent.parent.parent / "rules.json"


def load_rules() -> list:
    if not RULES_FILE.exists():
        return []
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def save_rules(rules: list) -> None:
    RULES_FILE.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_NEGATION_PREFIXES = ("没有", "无", "没", "看不到", "未发现", "不见")


def _is_negated(trigger: str, text: str) -> bool:
    for prefix in _NEGATION_PREFIXES:
        if prefix + trigger in text:
            return True
    return False


def check_rules(observation: str) -> list[str]:
    rules = load_rules()
    now = time.time()
    triggered = []
    changed = False
    for rule in rules:
        trigger = rule["trigger"]
        if trigger in observation and not _is_negated(trigger, observation):
            if now - rule.get("last_triggered", 0) >= rule["cooldown_s"]:
                rule["last_triggered"] = now
                triggered.append(rule["action"])
                changed = True
    if changed:
        save_rules(rules)
    return triggered


# ── LLM 单例 ──────────────────────────────────────────────────────

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
_SPORT_CMD_BY_LOWER = {c.lower(): c for c in _VALID_SPORT_CMDS}


def _normalize_sport_action(action: str) -> str | None:
    """把 LLM 给的 action 归一成规范动作名，匹配不上返回 None。

    容错：LLM 常把工具名混进来（"go2_sport Hello"），剥前缀；大小写不敏感。
    """
    a = action.strip()
    if a.lower().startswith("go2_sport"):
        a = a[len("go2_sport"):].strip()
    return _SPORT_CMD_BY_LOWER.get(a.lower())

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
    canonical = _normalize_sport_action(cmd)
    if canonical is None:
        return f"未知动作 {cmd}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    await go2.send_command(canonical)
    return f"已执行 {canonical}"


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


_OBSERVE_SYSTEM_PROMPT = (
    "你是机器狗 Go2 的视觉感知模块。"
    "图像来自机器狗前置摄像头，视角较低（约 30cm 离地）。"
    "根据问题简洁作答."
)


async def go2_observe(question: str = "描述你看到的场景") -> str:
    from langchain_core.messages import SystemMessage
    frame_b64 = go2.latest_frame_b64()
    if not frame_b64:
        return "无可用视频帧，请确认 Go2 已连接且视频流正常"
    resp = await get_vision_llm().ainvoke([
        SystemMessage(content=_OBSERVE_SYSTEM_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
        ]),
    ])
    return resp.content


def go2_status() -> str:
    if not go2.is_connected:
        return "Go2 未连接"
    s = go2._robot_state
    return f"已连接 | mode={s.get('mode')} body_height={s.get('body_height')} velocity={s.get('velocity')}"


def go2_add_rule(trigger: str, action: str, cooldown_s: int = 30) -> str:
    canonical = _normalize_sport_action(action)
    if canonical is None:
        return f"不支持的动作 {action}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    action = canonical
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


# ── 导航工具 ─────────────────────────────────────────────────────

async def go2_tag_location(name: str) -> str:
    if not go2.is_connected:
        return "Go2 未连接"
    odom = go2.odom
    if not odom:
        return "暂无位置数据，请确认 Odom 订阅正常"
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    return spatial_memory.tag_location(name, odom)


async def go2_navigate_to(name: str) -> str:
    if not go2.is_connected:
        return "Go2 未连接"
    from cloud.go2.navigation.navigator import navigator
    return await navigator.go_to(name)


def go2_list_locations() -> str:
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    locs = spatial_memory.list_locations()
    if not locs:
        return "暂无保存的地点，请先用 go2_tag_location 保存地点"
    return "\n".join(
        f"- {loc['name']} ({loc['x']:.2f}, {loc['y']:.2f})" for loc in locs
    )


async def go2_set_obstacle_avoidance(enabled: bool) -> str:
    if not go2.is_connected:
        return "Go2 未连接"
    await go2.set_obstacle_avoidance(enabled)
    return f"内置避障已{'开启' if enabled else '关闭'}"


async def go2_set_led(color: str = "white", duration: int = 60) -> str:
    if not go2.is_connected:
        return "Go2 未连接"
    try:
        await go2.set_led(color, duration)
        return f"LED 已设为 {color}，持续 {duration}s"
    except ValueError as e:
        return str(e)


# ── 工具分发表 ────────────────────────────────────────────────────

TOOL_FN_MAP = {
    "go2_sport":                  go2_sport,
    "go2_move":                   go2_move,
    "go2_observe":                go2_observe,
    "go2_status":                 go2_status,
    "go2_add_rule":               go2_add_rule,
    "go2_list_rules":             go2_list_rules,
    "go2_tag_location":           go2_tag_location,
    "go2_navigate_to":            go2_navigate_to,
    "go2_list_locations":         go2_list_locations,
    "go2_set_obstacle_avoidance": go2_set_obstacle_avoidance,
    "go2_set_led":                go2_set_led,
}

TOOL_DESCRIPTIONS = """\
可用工具：
- go2_sport(cmd): 执行预定义动作。cmd取值必须严格在下列范围内:StandUp/StandDown/StopMove/Hello/Stretch/Dance1/Dance2
- go2_move(direction, speed=0.3, duration=1.0): 移动机器狗。direction: forward/backward/left/right/turn_left/turn_right
- go2_observe(question="描述你看到的场景"): 用摄像头分析当前画面，返回视觉描述
- go2_status(): 查询连接状态和机器狗当前姿态
- go2_add_rule(trigger, action, cooldown_s=30): 添加视觉触发规则，检测到 trigger 关键词时自动执行 action(同 go2_sport 的 cmd 取值）
- go2_list_rules(): 列出当前所有视觉触发规则
- go2_tag_location(name): 将当前位置保存为命名地点，供导航使用
- go2_navigate_to(name): 导航到已保存的命名地点，支持模糊描述
- go2_list_locations(): 列出所有已保存的命名地点
- go2_set_obstacle_avoidance(enabled): 开启/关闭 Go2 内置避障（布尔值）
- go2_set_led(color="white", duration=60): 设置 LED 颜色，支持 white/red/yellow/blue/green/cyan/purple"""
