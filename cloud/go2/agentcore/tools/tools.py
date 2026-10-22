import asyncio
import json
import os
import time
import math
from dataclasses import asdict, is_dataclass
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from cloud.go2.connection import go2
from cloud.go2.paths import RULES_FILE
from cloud.go2.control import control_lease
from cloud.go2.execution import current_context, require_admin, require_persist

# ── 规则存储 ──────────────────────────────────────────────────────


def load_rules() -> list:
    if not RULES_FILE.exists():
        return []
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def save_rules(rules: list) -> None:
    require_persist("modify trained rules")
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _migrate_rule(rule: dict) -> dict:
    """把旧记录 {trigger, action, cooldown_s, last_triggered} 补全为富 schema。"""
    rule.setdefault("id", f"tb_{int(time.time() * 1000)}")
    rule.setdefault("behavior", rule.get("action", ""))
    rule.setdefault("action_hint", {"skill": rule.get("action", "")})
    rule.setdefault("enabled", True)
    rule.setdefault("created_ts", time.time())
    rule.setdefault("hit_count", 0)
    rule.setdefault("last_fired_ts", None)
    return rule


_NEGATION_PREFIXES = ("没有", "无", "没", "看不到", "未发现", "不见")


def _is_negated(trigger: str, text: str) -> bool:
    for prefix in _NEGATION_PREFIXES:
        if prefix + trigger in text:
            return True
    return False


def check_rules(observation: str, *, include_rules: bool = False) -> list:
    """Match and reserve cooldown; successful execution is recorded separately."""
    rules = [_migrate_rule(r) for r in load_rules()]
    now = time.time()
    triggered: list[str] = []
    changed = False
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        trigger = rule["trigger"]
        if trigger in observation and not _is_negated(trigger, observation):
            if now - rule.get("last_triggered", 0) >= rule["cooldown_s"]:
                rule["last_triggered"] = now
                triggered.append(dict(rule) if include_rules else rule["action"])
                changed = True
    if changed:
        save_rules(rules)
    return triggered


def record_rule_completion(rule_id: str) -> None:
    rules = load_rules()
    for rule in rules:
        if rule.get("id") == rule_id:
            rule["hit_count"] = rule.get("hit_count", 0) + 1
            rule["last_fired_ts"] = time.time()
            save_rules(rules)
            return


# ── LLM 单例 ──────────────────────────────────────────────────────

_text_llm = None
_vision_llm = None


def get_text_llm() -> ChatOpenAI:
    global _text_llm
    if _text_llm is None:
        _text_llm = ChatOpenAI(
            model=os.getenv("GO2_TEXT_MODEL", "deepseek-chat"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
            timeout=30,
        )
    return _text_llm


def get_vision_llm() -> ChatOpenAI:
    global _vision_llm
    if _vision_llm is None:
        _vision_llm = ChatOpenAI(
            model=os.getenv("GO2_VISION_MODEL", "qwen"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
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
    if canonical != "StopMove":
        control_lease.acquire(current_context())
    result = await go2.send_command(canonical)
    if not isinstance(result, dict):
        return f"已执行 {canonical}"
    status = result.get("result", "error")
    if status == "ok":
        return f"已执行 {canonical}"
    if status == "accepted":
        return f"指令已发送 {canonical}，尚未确认完成"
    raise RuntimeError(result.get("error") or f"{canonical} 执行未完成（{status}）")


async def go2_move(direction: str, speed: float = 0.3, duration: float = 1.0) -> str:
    if not go2.is_connected:
        return "Go2 未连接，请先建立连接"
    if direction not in _DIRECTION_MAP:
        return f"未知方向 {direction}，支持: {', '.join(_DIRECTION_MAP)}"
    if not math.isfinite(duration) or not 0 < duration <= 10:
        raise ValueError("duration must be in (0, 10] seconds")
    control_lease.acquire(current_context())
    params = _DIRECTION_MAP[direction](speed)
    await go2.send_command("Move", params)
    try:
        await asyncio.sleep(duration)
    finally:
        stopped = await go2.send_command("StopMove")
    return {"result": "accepted", "action": "Move", "stop": stopped,
            "message": "定时移动和停止指令已发送，实际位移未确认"}


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


def go2_add_rule(trigger: str, action: str, cooldown_s: int = 30,
                 behavior: str = "") -> str:
    """添加视觉触发调教：检测到 trigger 时执行 action（go2_sport 的 cmd 取值）。"""
    require_persist("add a trained rule")
    canonical = _normalize_sport_action(action)
    if canonical is None:
        return f"不支持的动作 {action}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    action = canonical
    rules = [_migrate_rule(r) for r in load_rules()]
    rules = [r for r in rules if not (r["trigger"] == trigger and r["action"] == action)]
    rules.append({
        "id":            f"tb_{int(time.time() * 1000)}",
        "trigger":       trigger,
        "action":        action,
        "behavior":      behavior or action,
        "action_hint":   {"skill": action},
        "cooldown_s":    cooldown_s,
        "last_triggered": 0,
        "enabled":       True,
        "created_ts":    time.time(),
        "hit_count":     0,
        "last_fired_ts": None,
    })
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
    require_persist("save a location")
    if not go2.is_connected:
        return "Go2 未连接"
    odom = go2.odom
    if not odom:
        return "暂无位置数据，请确认 Odom 订阅正常"
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    return spatial_memory.tag_location(name, odom)


async def go2_navigate_to(name: str) -> str:
    if not go2.is_connected:
        raise RuntimeError("Go2 未连接")
    context = current_context()
    control_lease.acquire(context)
    from cloud.go2.navigation.navigator import navigator
    try:
        result = await asyncio.wait_for(
            navigator.go_to(name, owner_session_id=context.session_id), timeout=25.0
        )
        return {"result": "ok" if result.startswith("已到达") else "error", "message": result}
    except asyncio.TimeoutError:
        return {"result": "timeout", "error": "navigation_timeout"}


def go2_list_locations() -> str:
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    locs = spatial_memory.list_locations()
    if not locs:
        return "暂无保存的地点，请先用 go2_tag_location 保存地点"
    return "\n".join(
        f"- {loc['name']} ({loc['x']:.2f}, {loc['y']:.2f})" for loc in locs
    )


async def go2_set_led(color: str = "white", duration: int = 60) -> str:
    if not go2.is_connected:
        raise RuntimeError("Go2 未连接")
    await go2.set_led(color, duration)
    return {"result": "accepted", "message": "机身灯指令已发送，未确认物理灯状态"}


# ── 身份工具 ──────────────────────────────────────────────────────

async def go2_recognize_person() -> dict:
    from cloud.go2.agentcore.skills.identity import identity_skill
    snapshot = await identity_skill.recognize_now()
    return _identity_result(snapshot)


def go2_list_people() -> dict:
    require_admin("read space people")
    from cloud.space.people.service import space_people
    people = [
        {"person_id": p.person_id, "name": p.name, "role": p.role, "face_count": p.face_count}
        for p in space_people.list_people()
    ]
    return {"result": "ok", "people": people}


def _identity_result(snapshot) -> dict:
    payload = asdict(snapshot) if is_dataclass(snapshot) else dict(vars(snapshot))
    payload["result"] = {
        "recognized": "ok",
        "unknown": "ok",
        "candidate": "ok",
        "idle": "ok",
        "rejected": "rejected",
        "disabled": "disabled",
        "error": "error",
    }.get(snapshot.status, snapshot.status)
    return payload


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
    "go2_set_led":                go2_set_led,
    "go2_recognize_person":       go2_recognize_person,
    "go2_list_people":            go2_list_people,
}

TOOL_DESCRIPTIONS = """\
可用工具：
- go2_sport(cmd): 执行预定义动作。cmd 取值及含义：StandUp（站起来）/StandDown（趴下/安静休息/待命）/StopMove（停止移动）/Hello（打招呼）/Stretch（伸展）/Dance1/Dance2（跳舞）
- go2_move(direction, speed=0.3, duration=1.0): 移动机器狗。direction: forward/backward/left/right/turn_left/turn_right
- go2_observe(question="描述你看到的场景"): 用摄像头分析当前画面，返回视觉描述
- go2_status(): 查询连接状态和机器狗当前姿态
- go2_add_rule(trigger, action, cooldown_s=30): 添加视觉触发规则，检测到 trigger 关键词时自动执行 action(同 go2_sport 的 cmd 取值）
- go2_list_rules(): 列出当前所有视觉触发规则
- go2_tag_location(name): 将当前位置保存为命名地点，供导航使用
- go2_navigate_to(name): 导航到已保存的命名地点，支持模糊描述
- go2_list_locations(): 列出所有已保存的命名地点
- go2_set_led(color="white", duration=60): 设置 LED 颜色，支持 white/red/yellow/blue/green/cyan/purple
- go2_recognize_person(): 立即识别面前的人；“面前是谁/你认识我吗”只能识别，不能隐式登记
- go2_list_people(): 列出空间已知人员和关系；人员增删请在手机的空间人员库中操作"""
