import asyncio
import json
import time
import math
import uuid
from dataclasses import asdict, is_dataclass

from langchain_core.messages import HumanMessage

from cloud.go2.connection import go2
from cloud.go2.paths import RULES_FILE
from cloud.go2.control import control_lease
from cloud.go2.execution import current_context, require_admin, require_persist

# Compatibility wrappers for existing callers; implementation lives in agentcore.rules.
from cloud.go2.agentcore import rules as _rules
from cloud.go2.agentcore.llm import get_text_llm, get_vision_llm

def load_rules():
    return _rules.load_rules(rules_file=RULES_FILE)

def save_rules(rules):
    return _rules.save_rules(rules, rules_file=RULES_FILE)

def _migrate_rule(rule):
    return _rules._migrate_rule(rule)

def check_rules(observation, *, include_rules=False):
    return _rules.check_rules(observation, include_rules=include_rules, rules_file=RULES_FILE)

def record_rule_completion(rule_id):
    return _rules.record_rule_completion(rule_id, rules_file=RULES_FILE)

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
        return {"result": "error", "error": "invalid_device_result", "action": canonical}
    status = result.get("result", "error")
    if status == "ok":
        return {"result": "ok", "action": canonical, "message": f"已执行 {canonical}"}
    if status == "accepted":
        return {"result": "accepted", "action": canonical, "message": f"指令已发送 {canonical}，尚未确认完成"}
    return {"result": status, "action": canonical, "error": result.get("error") or f"{canonical} 执行未完成"}


async def go2_move(direction: str, speed: float = 0.3, duration: float = 1.0) -> str:
    if not go2.is_connected:
        return "Go2 未连接，请先建立连接"
    if direction not in _DIRECTION_MAP:
        return f"未知方向 {direction}，支持: {', '.join(_DIRECTION_MAP)}"
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0 < speed <= 0.5:
        raise ValueError("speed must be in (0, 0.5]")
    if not math.isfinite(duration) or not 0 < duration <= 10:
        raise ValueError("duration must be in (0, 10] seconds")
    control_lease.acquire(current_context())
    task_id = uuid.uuid4().hex
    params = _DIRECTION_MAP[direction](speed)
    started = await go2.send_command("Move", params)
    if not isinstance(started, dict) or started.get("result") not in ("ok", "accepted"):
        return {"result": "error", "task_id": task_id, "action": "Move", "start": started,
                "error": "move_not_accepted"}
    try:
        await asyncio.sleep(duration)
    finally:
        owner = control_lease.snapshot()
        if owner is not None and owner.owner_session_id != current_context().session_id:
            stopped = {"result": "error", "error": "lease_transferred"}
        else:
            try:
                stopped = await go2.send_command("StopMove")
            except Exception as exc:
                stopped = {"result": "error", "error": type(exc).__name__}
    stop_status = stopped.get("result") if isinstance(stopped, dict) else None
    if stop_status not in ("ok", "accepted"):
        return {"result": "timeout" if stop_status == "timeout" else "error",
                "task_id": task_id, "action": "Move", "start": started, "stop": stopped,
                "error": "stop_not_confirmed", "message": "停止指令未确认，需检查设备状态"}
    return {"result": "accepted", "task_id": task_id, "action": "Move", "start": started, "stop": stopped,
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
    if (not odom or go2.odom_received_at is None or
            time.monotonic() - go2.odom_received_at > 1.0 or
            go2.odom_frame_id != "odom"):
        return "暂无位置数据，请确认 Odom 订阅正常"
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    return spatial_memory.tag_location(name, odom)


async def go2_navigate_to(name: str) -> dict:
    if not go2.is_connected:
        raise RuntimeError("Go2 未连接")
    context = current_context()
    control_lease.acquire(context)
    from cloud.go2.navigation.navigator import navigator
    result = await navigator.go_to(name, owner_session_id=context.session_id)
    return {"result": "ok" if result["status"] == "succeeded" else
            "error" if result["status"] == "failed" else result["status"], **result}


async def go2_navigate_to_coordinate(x: float, y: float) -> dict:
    if not go2.is_connected:
        raise RuntimeError("Go2 未连接")
    context = current_context()
    control_lease.acquire(context)
    from cloud.go2.navigation.navigator import navigator
    result = await navigator.go_to_coordinate(x, y, owner_session_id=context.session_id)
    return {"result": "ok" if result["status"] == "succeeded" else
            "error" if result["status"] == "failed" else result["status"], **result}


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
    from cloud.go2.agentcore.perception.identity import identity_skill
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


def list_peers() -> dict:
    """Read the gateway's space-filtered view using this tool's trusted identity."""
    from cloud.peers.gateway import list_visible_peers
    return {"result": "ok", "peers": list_visible_peers("go2")}


def get_space_context() -> dict:
    from cloud.api.main import _esp32_state
    from cloud.space.context import get_space_context as read_space
    return {"result": "ok", "space": read_space("go2", state=_esp32_state)}


def send_message(target: str, content: str, reply_to: str | None = None) -> dict:
    """Send an agent message under this process's fixed Go2 identity."""
    from cloud.api.main import get_agent_messenger
    messenger = get_agent_messenger()
    if messenger is None:
        return {"result": "error", "error": "message_bus_unavailable"}
    context = current_context()
    if context.origin_kind == "user_delegated" and context.expires_at <= time.time():
        return {"result": "blocked", "error": "session_expired"}
    return messenger.send_message("go2", target, content, reply_to)


async def request_peer_skill(target_unit_id: str, skill_id: str, params: dict,
                             request_id: str | None = None) -> dict:
    """Ask the trusted gateway for one peer action; never trust model role fields."""
    from cloud.peers.gateway import request_peer_skill as dispatch_peer_skill
    context = current_context()
    origin_kind = getattr(context, "origin_kind", "user_delegated")
    return await asyncio.to_thread(
        dispatch_peer_skill,
        initiator_unit_id="go2",
        target_unit_id=target_unit_id,
        skill_id=skill_id,
        params=params,
        context=context,
        origin_kind=origin_kind,
        request_id=request_id,
    )


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
    "go2_navigate_to_coordinate": go2_navigate_to_coordinate,
    "go2_list_locations":         go2_list_locations,
    "go2_set_led":                go2_set_led,
    "go2_recognize_person":       go2_recognize_person,
    "go2_list_people":            go2_list_people,
    "list_peers":                 list_peers,
    "get_space_context":          get_space_context,
    "send_message":               send_message,
    "request_peer_skill":         request_peer_skill,
}

from cloud.go2.agentcore.tools.registry import tool_descriptions

TOOL_DESCRIPTIONS = tool_descriptions()
