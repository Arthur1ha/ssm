import json
import time
from dataclasses import asdict, is_dataclass

from langchain_core.messages import HumanMessage

from cloud.go2.connection import go2
from cloud.go2.actions import (SPORT_COMMANDS, move_tool_schema,
                               validate_move_goal,
                               LED_DEFAULT_COLOR, LED_DEFAULT_DURATION)
from cloud.go2.control import control_lease
from cloud.go2.execution import current_context, require_admin, require_persist

from cloud.go2.models import get_text_llm, get_vision_llm

# ── 工具函数 ──────────────────────────────────────────────────────

_VALID_SPORT_CMDS = frozenset(SPORT_COMMANDS)
_SPORT_CMD_BY_LOWER = {c.lower(): c for c in _VALID_SPORT_CMDS}


def _normalize_sport_action(action: str) -> str | None:
    """把 LLM 给的 action 归一成规范动作名，匹配不上返回 None。

    容错：LLM 常把工具名混进来（"go2_sport Hello"），剥前缀；大小写不敏感。
    """
    a = action.strip()
    if a.lower().startswith("go2_sport"):
        a = a[len("go2_sport"):].strip()
    return _SPORT_CMD_BY_LOWER.get(a.lower())

async def go2_sport(cmd: str) -> dict | str:
    if not go2.is_connected:
        return "Go2 未连接，请先建立连接"
    canonical = _normalize_sport_action(cmd)
    if canonical is None:
        return f"未知动作 {cmd}，支持: {', '.join(sorted(_VALID_SPORT_CMDS))}"
    from cloud.go2.tools.executor import execute_action
    result = await execute_action(canonical, context=current_context())
    if not isinstance(result, dict):
        return {"result": "error", "error": "invalid_device_result", "action": canonical}
    status = result.get("result", "error")
    # Keep the runtime's request/completion scope and uncertainty intact.
    payload = {**result, "action": canonical}
    if status == "ok":
        payload["message"] = f"已执行 {canonical}"
        return payload
    if status == "accepted":
        payload["message"] = f"指令已发送 {canonical}，尚未确认完成"
        return payload
    payload["error"] = result.get("error") or f"{canonical} 执行未完成"
    return payload


async def go2_move(direction: str, distance_m: float | None = None,
                   angle_deg: float | None = None, speed: float | None = None) -> dict:
    from cloud.go2.tools.executor import validate_params
    from cloud.go2.navigation.navigator import navigator
    params = {"direction": direction, **{key: value for key, value in {
        "distance_m": distance_m, "angle_deg": angle_deg, "speed": speed}.items()
        if value is not None}}
    validate_params(move_tool_schema(), params)
    validate_move_goal(params)
    speed = move_tool_schema()["properties"]["speed"]["default"] if speed is None else speed
    return await navigator.move_relative(direction, distance_m=distance_m,
        angle_deg=angle_deg, speed=speed, context=current_context())


_OBSERVE_SYSTEM_PROMPT = (
    "你是机器狗 Go2 的视觉感知模块。"
    "图像来自机器狗前置摄像头，视角较低（约 30cm 离地）。"
    "只报告画面可见的事实；单张图像不能确认雷达地图的通行空间，"
    "即使问题询问可达性，也不要断言目标可达或已到达。"
    "根据问题简洁作答。"
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


async def go2_life(action: str) -> dict:
    from cloud.go2.service import perform_life_control
    return await perform_life_control(action, current_context())


def go2_status() -> dict:
    return {"result": "ok", **go2.state_snapshot(), "telemetry": go2._robot_state}


# ── 导航工具 ─────────────────────────────────────────────────────

_VISUAL_NAV_BLOCK_MESSAGES = {
    "calibration_unavailable": "当前 Go2 的实测相机标定缺失或未通过校验，无法确定画面目标的位置；本次任务未发出移动指令。",
}

async def go2_tag_location(name: str) -> str:
    require_persist("save a location")
    if not go2.is_connected:
        return "Go2 未连接"
    odom = go2.odom
    if (not odom or go2.odom_received_at is None or
            time.monotonic() - go2.odom_received_at > 1.0 or
            go2.odom_frame_id != "odom"):
        return "暂无位置数据，请确认 Odom 订阅正常"
    from cloud.agents.go2.memory import spatial as spatial_memory
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


async def go2_navigate_to_visible_target(target: str) -> dict:
    """Fuse the visual target with LiDAR and navigate to a mapped stop cell."""
    from cloud.go2.navigation.target_approach import navigate_to_visible_target
    return await navigate_to_visible_target(target)


async def go2_find_object(target: str) -> dict:
    """Await the one server-owned search graph, including final arrival proof."""
    from cloud.agents.go2.workflows.search import run_search
    return await run_search(target)


def go2_list_locations() -> str:
    from cloud.agents.go2.memory import spatial as spatial_memory
    locs = spatial_memory.list_locations()
    if not locs:
        return "暂无保存的地点，请先用 go2_tag_location 保存地点"
    return "\n".join(
        f"- {loc['name']} ({loc['x']:.2f}, {loc['y']:.2f})" for loc in locs
    )


async def go2_set_led(color: str = LED_DEFAULT_COLOR, duration: int = LED_DEFAULT_DURATION) -> str:
    if not go2.is_connected:
        raise RuntimeError("Go2 未连接")
    await go2.set_led(color, duration)
    return {"result": "accepted", "message": "机身灯指令已发送，未确认物理灯状态"}


# ── 身份工具 ──────────────────────────────────────────────────────

async def go2_recognize_person() -> dict:
    from cloud.go2.perception.identity import identity_skill
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


def send_message(target: str, content: str, reply_to: str | None = None,
                 interaction_kind: str = "message", game_id: str | None = None) -> dict:
    """Send an agent message under this process's fixed Go2 identity."""
    from cloud.api.main import get_agent_messenger
    messenger = get_agent_messenger()
    if messenger is None:
        return {"result": "error", "error": "message_bus_unavailable"}
    context = current_context()
    if context.origin_kind == "user_delegated" and context.expires_at <= time.time():
        return {"result": "blocked", "error": "session_expired"}
    return messenger.send_message("go2", target, content, reply_to,
                                  interaction_kind=interaction_kind, game_id=game_id)


async def request_peer_skill(target_unit_id: str, skill_id: str, params: dict,
                             request_id: str | None = None) -> dict:
    """Ask the trusted gateway for one peer action; never trust model role fields."""
    from cloud.peers.gateway import request_peer_skill as dispatch_peer_skill
    context = current_context()
    origin_kind = getattr(context, "origin_kind", "user_delegated")
    from cloud.agents.common.runner import invoke_blocking
    return await invoke_blocking(lambda: dispatch_peer_skill(
        initiator_unit_id="go2", target_unit_id=target_unit_id,
        skill_id=skill_id, params=params, context=context,
        origin_kind=origin_kind, request_id=request_id))


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
    "go2_life":                   go2_life,
    "go2_tag_location":           go2_tag_location,
    "go2_navigate_to":            go2_navigate_to,
    "go2_navigate_to_coordinate": go2_navigate_to_coordinate,
    "go2_navigate_to_visible_target": go2_navigate_to_visible_target,
    "go2_find_object": go2_find_object,
    "go2_list_locations":         go2_list_locations,
    "go2_set_led":                go2_set_led,
    "go2_recognize_person":       go2_recognize_person,
    "go2_list_people":            go2_list_people,
    "list_peers":                 list_peers,
    "get_space_context":          get_space_context,
    "send_message":               send_message,
    "request_peer_skill":         request_peer_skill,
}

from cloud.go2.tools.registry import tool_descriptions

TOOL_DESCRIPTIONS = tool_descriptions()
