"""cloud.go2.router — Go2 机器狗 FastAPI 路由。

负责 Go2 连接/断开/控制的全部 HTTP 端点，以及连接状态变更时的
MQTT 控制面管理（发布/清除 retained card）。
"""

import asyncio
import json
import logging
import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from cloud.go2.connection import go2
from cloud.go2.navigation.drive import drive
from cloud.go2.agentcore.skills.reactive import reactive_mind
from cloud.go2.agentcore.skills.vision import vision_loop
from cloud.go2.agentcore.skills.identity import IdentityEvent, identity_skill
from cloud.go2.agentcore.identity.provider import FaceProviderError
from cloud.go2.agentcore.memory.episode import EventType, episode_memory
from cloud.go2.control import LeaseConflict, control_lease, session_tasks
from cloud.go2.execution import (
    ExecutionContext, SYSTEM_CONTEXT, current_context, require_admin_read,
    require_authenticated_read, require_service_context, use_context,
)

# MQTT 客户端，由 api/main.py 通过 init_mqtt() 注入
_mqtt_client = None

GO2_CARD_TOPIC = "ssm/agents/go2/card"
GO2_STATUS_TOPIC = "ssm/agents/go2/status"


def init_mqtt(client) -> None:
    """注入 api 进程的 MQTT 客户端，供发布/清除 Go2 card 使用。"""
    global _mqtt_client
    _mqtt_client = client


_GO2_FSM_LABELS = {
    "StandUp": "起立", "StandDown": "趴下休息", "EnterMoveControl": "移动控制", "Move": "移动", "StopMove": "退出移动控制",
    "Hello": "挥手", "Stretch": "伸展", "Dance1": "舞蹈1", "Dance2": "舞蹈2",
}


def _go2_state_machine() -> dict:
    """依据 connection/fsm.py 的 _FSM_NEXT 生成静态状态机拓扑（单一真相）。

    延迟导入以避免在测试 stub 环境中与 cloud.go2.connection 补丁冲突。
    """
    from cloud.go2.connection.fsm import _FSM_AVAILABLE, _FSM_NEXT, _TRANSIENT_STATES  # noqa: PLC0415
    transitions = []
    for src, edges in _FSM_NEXT.items():
        for trigger, dst in edges.items():
            if trigger == "Move":
                continue  # 有速度的 Move 属于数据面；图上只显示控制状态入口。
            transitions.append({
                "src": src, "dst": dst, "trigger": trigger,
                "label": "结束动作" if src in _TRANSIENT_STATES else _GO2_FSM_LABELS.get(trigger, trigger),
                "action": trigger,
                "params": {},
            })
    return {
        "states": [s for s in _FSM_AVAILABLE if s not in {"offline", "connecting"}],
        "transitions": transitions,
        "initial": "offline",
        "views": {
            "offline": {"label": "未连接", "description": "机器狗尚未建立控制连接。", "type": "robot_pose", "pose": "offline"},
            "connecting": {"label": "正在连接", "description": "正在与机器狗建立实时控制通道。", "type": "robot_pose", "pose": "offline"},
            "lying": {"label": "趴下休息", "description": "身体降低并保持稳定，等待起立指令。", "type": "robot_pose", "pose": "lying"},
            "standing": {"label": "站立待命", "description": "四足稳定站立，可以移动或执行动作。", "type": "robot_pose", "pose": "standing"},
            "moving": {"label": "移动控制", "description": "拖动摇杆行走；松开后停下，退出控制才回到站立。", "type": "robot_pose", "pose": "moving"},
            "greeting": {"label": "挥手问候", "description": "抬起前腿执行问候动作。", "type": "robot_pose", "pose": "greeting"},
            "stretching": {"label": "正在伸展", "description": "降低身体前部并完成伸展动作。", "type": "robot_pose", "pose": "stretching"},
            "dancing1": {"label": "舞蹈动作一", "description": "正在执行第一组节奏动作。", "type": "robot_pose", "pose": "dance-left"},
            "dancing2": {"label": "舞蹈动作二", "description": "正在执行第二组节奏动作。", "type": "robot_pose", "pose": "dance-right"},
        },
    }


def _build_go2_card() -> dict:
    """构建 Go2 Agent Card dict（静态能力描述，符合 AgentCard schema）。

    card 只含能力，不含 volatile state；online/status 表示真实 WebRTC 连接状态，
    FSM/动作等动态状态经 HTTP /api/go2/* 实时获取。
    """
    return {
        "unit_id": "go2",
        "device_id": "go2",
        "name": "Go2 机器狗",
        "description": "四足机器人，支持运动控制、导航和视觉感知",
        "agent_type": "robot",
        "online": bool(go2.is_connected),
        "transport": {
            "kind": "http",
            "endpoint": "/api/go2/chat",
            "command_endpoint": "/api/go2/commands",
            "state_stream": "/api/go2/connection/stream",
        },
        "skills": [
            {
                "id": "go2_chat",
                "name": "对话 / 自然语言控制",
                "tags": ["conversation", "robot"],
                "params_schema": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string"}},
                },
                "invoke": {"action": "CHAT"},
            },
            {
                "id": "go2_sport",
                "name": "预定义动作",
                "tags": ["motion", "robot"],
                "params_schema": {
                    "type": "object",
                    "required": ["cmd"],
                    "properties": {
                        "cmd": {
                            "enum": ["StandUp", "StandDown", "Hello", "Stretch", "Dance1", "Dance2"]
                        }
                    },
                },
                "invoke": {"action": "SPORT"},
            },
            {
                "id": "go2_navigate",
                "name": "导航到命名地点",
                "tags": ["navigation", "robot"],
                "params_schema": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                "invoke": {"action": "NAVIGATE"},
            },
            {
                "id": "go2_recognize_person",
                "name": "识别面前的人",
                "tags": ["vision", "identity", "robot"],
                "params_schema": {"type": "object", "properties": {}},
                "invoke": {"action": "RECOGNIZE_PERSON"},
            },
        ],
        # 动态状态不进 card（card=静态能力）；FSM/动作经 HTTP /api/go2/* 实时获取
        "state": {},
        "state_machine": _go2_state_machine(),
        "modes": [{
            "id": "autonomy",
            "label": "自主性",
            "options": [
                {"value": "manual",       "label": "完全遥控", "description": "由用户/编排器遥控决定行动"},
                {"value": "reactive",     "label": "自主反应", "description": "机器狗根据视觉输入自主做即时反应"},
                {"value": "free_explore", "label": "自由探索", "description": "机器狗自主探索环境"},
            ],
            "get": "/api/go2/autonomy",
            "set": "/api/go2/autonomy",
        }],
        "telemetry": [
            {"key": "fsm_state",   "label": "动作状态"},
            {"key": "body_height", "label": "身高", "unit": "m"},
            {"key": "velocity",    "label": "速度", "unit": "m/s"},
        ],
        "widgets": [
            {
                "type": "connection",
                "states": ["offline", "connecting"],
                "endpoint": "/api/go2/connection",
                "status_endpoint": "/api/go2/connection",
                "auto_connect": True,
                "visible": False,
            },
            {"type": "video",    "states": ["lying", "standing", "moving", "greeting", "stretching", "dancing1", "dancing2"], "endpoint": "/api/go2/video"},
            {"type": "joystick", "states": ["moving"],    "endpoint": "/api/go2/velocity"},
        ],
    }


def _publish_go2_card() -> None:
    """向 MQTT broker 发布 Go2 card（retained=True，qos=1）。

    若 MQTT 客户端尚未注入则静默失败，不影响连接流程。
    """
    if _mqtt_client is None:
        logging.warning("[Go2] MQTT 客户端未注入，跳过 card 发布")
        return
    try:
        card = _build_go2_card()
        _mqtt_client.publish(GO2_CARD_TOPIC, json.dumps(card, ensure_ascii=False), retain=True, qos=1)
        logging.info("[Go2] 已发布 retained card 到 %s", GO2_CARD_TOPIC)
    except Exception as exc:
        logging.error("[Go2] 发布 card 失败: %s", exc)


def _publish_go2_status(online: bool | None = None) -> None:
    """发布 Go2 真实连接状态（retained，API 进程 LWT 会兜底置 offline）。"""
    if _mqtt_client is None:
        return
    try:
        if online is None:
            online = bool(go2.is_connected)
        _mqtt_client.publish(GO2_STATUS_TOPIC, "online" if online else "offline", retain=True, qos=1)
        logging.info("[Go2] status → %s", "online" if online else "offline")
    except Exception as exc:
        logging.error("[Go2] 发布 status 失败: %s", exc)


def _clear_go2_card() -> None:
    """清除 broker 上的 Go2 retained card（发布空 payload，qos=1）。

    若 MQTT 客户端尚未注入则静默失败，不影响断开流程。
    """
    if _mqtt_client is None:
        logging.warning("[Go2] MQTT 客户端未注入，跳过 card 清除")
        return
    try:
        _mqtt_client.publish(GO2_CARD_TOPIC, "", retain=True, qos=1)
        logging.info("[Go2] 已清除 retained card: %s", GO2_CARD_TOPIC)
    except Exception as exc:
        logging.error("[Go2] 清除 card 失败: %s", exc)


router = APIRouter(prefix="/api/go2", tags=["go2"])

# 当前注册的视觉回调句柄，重连时先移除再重注册，防止重复触发
_active_rule_cb  = None
_active_drive_cb = None
_active_identity_cb = None
_autonomy_mode   = "manual"  # "manual" | "reactive" | "free_explore"
_connect_task: asyncio.Task | None = None

# Continuous identity vision is global device activity, not an administrator
# request. It must inherit a non-persistent context. Explicit admin-triggered
# recognition still uses that request's trusted context and may be remembered.
_IDENTITY_VISION_CONTEXT = ExecutionContext(
    "go2-identity-vision", "visitor", False,
)


def _normalize_context(context) -> ExecutionContext:
    """Direct Python calls are already trusted; HTTP always supplies a verified context."""
    return context if isinstance(context, ExecutionContext) else SYSTEM_CONTEXT


def _require_admin_persist(context: ExecutionContext) -> None:
    if context.actor != "admin" or not context.persist:
        raise HTTPException(status_code=403, detail="administrator persistence required")


def _acquire_motion(context: ExecutionContext) -> None:
    previous = control_lease.snapshot()
    try:
        control_lease.acquire(context)
    except LeaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "go2_control_busy",
                "message": "机器狗正在被其他体验者使用",
                "owner_session_id": exc.owner_session_id,
                "expires_at": exc.expires_at,
            },
        ) from exc
    if (
        previous is not None
        and previous.owner_session_id != context.session_id
        and context.actor == "admin"
    ):
        from cloud.go2.navigation.navigator import navigator
        navigator.stop(owner_session_id=previous.owner_session_id)
        session_tasks.cancel(previous.owner_session_id)
        logging.info(
            "[Go2/Lease] admin %s preempted %s",
            context.session_id, previous.owner_session_id,
        )


def _on_identity_event(event: IdentityEvent) -> None:
    current = event.current
    previous = event.previous
    execution = current_context()
    # Fail closed for process-global callbacks. SYSTEM_CONTEXT is a convenient
    # default for trusted maintenance code, but it is not proof that an admin
    # request caused this visual event.
    may_record_episode = (
        execution.actor == "admin"
        and execution.persist
        and execution.session_id != SYSTEM_CONTEXT.session_id
    )
    identity_changed = (
        current.status == "recognized"
        and (previous.status != "recognized" or previous.person_id != current.person_id)
    )
    identity_absent = previous.status == "recognized" and current.status == "idle"
    if identity_changed and may_record_episode:
        episode_memory.add(
            EventType.IDENTITY_CHANGE,
            f"识别到{current.name}（{current.role}，置信分 {current.score:.1f}）",
        )
    elif identity_absent and may_record_episode:
        episode_memory.add(EventType.IDENTITY_CHANGE, f"{previous.name}已离开画面")

    if current.status in {"recognized", "unknown"} and _mqtt_client is not None:
        payload = {
            "type": "identity", "agent_id": "go2", "ts": current.ts,
            "status": current.status, "person_id": current.person_id,
            "name": current.name, "role": current.role, "score": current.score,
        }
        try:
            _mqtt_client.publish("ssm/agents/go2/thought", json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            logging.warning("[Go2/Identity] 发布识别事件失败: %s", exc)

    reactive_mind.on_identity_event(event)
    drive.on_identity_event(event)


def _on_identity_vision_frame(frame) -> None:
    """Run the global identity frame consumer with persistence disabled.

    The identity skill creates an async task here. Context variables are copied
    into that task, so the later identity-event callback remains non-persistent.
    """
    with use_context(_IDENTITY_VISION_CONTEXT):
        identity_skill.on_vision_frame(frame)


identity_skill.add_callback(_on_identity_event)


async def _initialize_identity_vision() -> None:
    """连接后先验证人员库，再决定是否启动身份视觉消费者。"""
    await identity_skill.check_provider()
    _configure_vision_consumers(_autonomy_mode)


def _configure_vision_consumers(mode: str, owner_session_id: str = "system") -> None:
    """统一重配视觉消费者；身份在三种 autonomy 模式下都可保持运行。"""
    global _active_rule_cb, _active_drive_cb, _active_identity_cb
    drive.stop()
    for callback in (_active_rule_cb, _active_drive_cb, _active_identity_cb):
        if callback is not None:
            vision_loop.remove_callback(callback)
    _active_rule_cb = _active_drive_cb = _active_identity_cb = None

    if not go2.is_connected:
        vision_loop.stop()
        identity_skill.clear_transient()
        return
    if identity_skill.enabled:
        _active_identity_cb = _on_identity_vision_frame
        vision_loop.add_callback(_active_identity_cb)
    if mode == "reactive":
        _active_rule_cb = reactive_mind.on_vision_frame
        vision_loop.add_callback(_active_rule_cb)
    elif mode == "free_explore":
        _active_drive_cb = drive.on_vision_frame
        vision_loop.add_callback(_active_drive_cb)
        drive.start(owner_session_id=owner_session_id)

    if any((_active_identity_cb, _active_rule_cb, _active_drive_cb)):
        vision_loop.start(lambda: go2._latest_frame)
    else:
        vision_loop.stop()


@router.post("/connection")
async def go2_connect(context: ExecutionContext = Depends(require_service_context)):
    global _connect_task
    if go2.is_connected:
        return {"status": "connected"}
    if _connect_task is not None and not _connect_task.done():
        return {"status": "connecting"}

    email    = os.getenv("GO2_EMAIL", "")
    password = os.getenv("GO2_PASSWORD", "")
    serial   = os.getenv("GO2_SERIAL", "")
    region   = os.getenv("GO2_REGION", "cn")
    if not email or not password or not serial:
        raise HTTPException(status_code=500, detail="GO2_EMAIL/PASSWORD/SERIAL 未配置")
    go2._last_error = None

    def _on_connect_done(t: asyncio.Task) -> None:
        global _autonomy_mode, _connect_task
        _connect_task = None
        if t.cancelled():
            _publish_go2_status(False)
            return
        if t.exception():
            go2._last_error = str(t.exception())
            _publish_go2_card()
            _publish_go2_status(False)
            return

        _publish_go2_card()
        _publish_go2_status()

        # 默认完全遥控，但已启用的身份消费者仍保持视觉检测。
        _autonomy_mode = "manual"
        if identity_skill.enabled:
            asyncio.create_task(_initialize_identity_vision())
        else:
            _configure_vision_consumers(_autonomy_mode)

        # Connection is operational state, not permission to overwrite Home.

    _publish_go2_status(False)
    _connect_task = asyncio.create_task(go2.connect(email, password, serial, region))
    _connect_task.add_done_callback(_on_connect_done)
    return {"status": "connecting"}


@router.delete("/connection")
async def go2_disconnect(context: ExecutionContext = Depends(require_service_context)):
    global _autonomy_mode
    await go2.disconnect()
    _configure_vision_consumers("manual")
    _autonomy_mode   = "manual"
    _publish_go2_card()
    _publish_go2_status(False)
    return {"status": "disconnected"}


@router.get("/connection")
async def go2_status(context=Depends(require_authenticated_read)):
    return {
        "connected":        go2.is_connected,
        "fsm_state":        go2.fsm_state,
        "available_actions": go2.available_actions,
        "state":            go2._robot_state,
        "error":            getattr(go2, "_last_error", None),
    }


@router.get("/video")
async def go2_video(context=Depends(require_authenticated_read)):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 not connected")
    return StreamingResponse(
        go2.mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/video/snapshot")
async def go2_video_snapshot(context=Depends(require_authenticated_read)):
    """单帧 JPEG，供不支持 MJPEG 的浏览器（iOS Safari）轮询使用。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    frame = go2._latest_frame
    if frame is None:
        raise HTTPException(status_code=503, detail="暂无画面")
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/connection/stream")
async def go2_state(context=Depends(require_authenticated_read)):
    async def sse_gen():
        q = go2.new_state_queue()
        try:
            while True:
                try:
                    state = await asyncio.wait_for(q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    state = {}
                payload = {
                    "connected": go2.is_connected,
                    "fsm_state": go2.fsm_state,
                    "available_actions": go2.available_actions,
                    "state": go2._robot_state,
                    "error": getattr(go2, "_last_error", None),
                }
                if isinstance(state, dict):
                    payload.update(state)
                payload["fsm_state"] = go2.fsm_state
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            go2.remove_state_queue(q)

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


class CommandRequest(BaseModel):
    cmd: str = ""
    action: str = ""   # 与 cmd 等价，通用 FSM 派发字段；两者取非空者
    params: dict = {}


class VelocityRequest(BaseModel):
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0


@router.post("/commands")
async def go2_command(req: CommandRequest, context: ExecutionContext = Depends(require_service_context)):
    cmd = req.action or req.cmd
    if not cmd:
        raise HTTPException(status_code=400, detail="cmd or action required")
    context = _normalize_context(context)
    if cmd != "StopMove":
        _acquire_motion(context)
    try:
        result = await go2.send_command(cmd, req.params or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return result


@router.post("/velocity")
async def go2_velocity(req: VelocityRequest, context: ExecutionContext = Depends(require_service_context)):
    """WIRELESS_CONTROLLER 速度控制，无 ack，适合摇杆实时输入。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    if any((req.vx, req.vy, req.vyaw)):
        _acquire_motion(context)
    try:
        result = go2.move_velocity(req.vx, req.vy, req.vyaw, deadman_s=2.0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logging.error("[Go2/Velocity] move_velocity 异常: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    return result


class ChatRequest(BaseModel):
    session_id: str | None = None  # untrusted compatibility field; ignored
    message: str
    skill_id: str | None = None
    params: dict | None = None


@router.post("/chat")
async def go2_chat(req: ChatRequest, context: ExecutionContext = Depends(require_service_context)):
    from cloud.go2.agent import run_agent
    context = _normalize_context(context)
    drive.user_interrupt = True
    task = asyncio.create_task(
        run_agent(
            context.session_id, req.message, skill_id=req.skill_id,
            params=req.params, context=context,
        ),
        name=f"go2-chat-{context.session_id}",
    )
    session_tasks.track(context.session_id, task)
    try:
        return await task
    finally:
        drive.user_interrupt = False


@router.post("/chat/stream")
async def go2_chat_stream(req: ChatRequest, context: ExecutionContext = Depends(require_service_context)):
    """SSE 流：逐步推送规划、工具执行、最终回复事件。"""
    from cloud.go2.agent import run_agent_stream

    context = _normalize_context(context)

    async def sse_gen():
        drive.user_interrupt = True
        stream_task = asyncio.current_task()
        if stream_task is not None:
            session_tasks.track(context.session_id, stream_task)
        try:
            async for event in run_agent_stream(context.session_id, req.message, context=context):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            drive.user_interrupt = False

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/mind/last_decision")
async def go2_mind_last_decision(context=Depends(require_authenticated_read)):
    """返回 ReactiveMind 上次 LLM 推理的决策结果。"""
    d = reactive_mind.last_decision
    if d is None:
        return {"decision": None}
    return {"decision": d}


@router.get("/vision")
async def go2_vision_latest(context=Depends(require_authenticated_read)):
    """返回最近一次 OpenCV 检测的结构化结果。"""
    if vision_loop.latest is None:
        raise HTTPException(status_code=503, detail="暂无检测数据，请确认 Go2 已连接")
    return vision_loop.latest


@router.get("/vision/stream")
async def go2_vision_stream(context=Depends(require_authenticated_read)):
    """SSE 流：每次检测完成后推送一条 VisionFrame JSON。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=10)

    def on_frame(result):
        try:
            q.put_nowait(result)
        except asyncio.QueueFull:
            pass

    vision_loop.add_callback(on_frame)

    async def generate():
        try:
            while go2.is_connected:
                try:
                    result = await asyncio.wait_for(q.get(), timeout=5.0)
                    yield f"data: {json.dumps(result)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {}\n\n"
        finally:
            vision_loop.remove_callback(on_frame)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 导航端点 ─────────────────────────────────────────────────────

class TagLocationRequest(BaseModel):
    name: str


class NavigateRequest(BaseModel):
    name: str


class PatrolRequest(BaseModel):
    stops: list[str]


class LedRequest(BaseModel):
    color: str = "white"
    duration: int = 60


class AutonomyRequest(BaseModel):
    mode: str  # "manual" | "reactive" | "free_explore"


class PersonalityRequest(BaseModel):
    prompt: str


class PersonCreateRequest(BaseModel):
    name: str
    role: str = "known"


@router.post("/navigation/locations")
async def nav_tag_location(req: TagLocationRequest, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    odom = go2.odom
    if not odom:
        raise HTTPException(status_code=503, detail="暂无 Odom 数据")
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    with use_context(context):
        result = spatial_memory.tag_location(req.name, odom)
    return {"ok": True, "message": result}


@router.get("/navigation/locations")
async def nav_list_locations(context=Depends(require_authenticated_read)):
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    return spatial_memory.list_locations()


@router.delete("/navigation/locations/{name}")
async def nav_delete_location(name: str, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    with use_context(context):
        deleted = spatial_memory.delete_location(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"地点「{name}」不存在")
    return {"ok": True}


@router.post("/navigation/go")
async def nav_go(req: NavigateRequest, context: ExecutionContext = Depends(require_service_context)):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    _acquire_motion(context)
    from cloud.go2.navigation.navigator import navigator
    task = asyncio.create_task(navigator.go_to(req.name, owner_session_id=context.session_id))
    session_tasks.track(context.session_id, task)
    return {"ok": True, "message": f"开始导航到「{req.name}」"}


@router.post("/navigation/patrol")
async def nav_start_patrol(req: PatrolRequest, context: ExecutionContext = Depends(require_service_context)):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if not req.stops:
        raise HTTPException(status_code=400, detail="stops 不能为空")
    context = _normalize_context(context)
    _acquire_motion(context)
    from cloud.go2.navigation.navigator import navigator
    await navigator.start_patrol(req.stops, owner_session_id=context.session_id)
    if navigator._task is not None:
        session_tasks.track(context.session_id, navigator._task)
    return {"ok": True, "stops": req.stops}


@router.delete("/navigation/patrol")
async def nav_stop(context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    from cloud.go2.navigation.navigator import navigator
    stopped = navigator.stop(owner_session_id=context.session_id)
    if not stopped and context.actor == "admin":
        navigator.stop()
        stopped = True
    return {"ok": True, "stopped": stopped}


@router.post("/stop")
async def emergency_stop(context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    global _autonomy_mode
    from cloud.go2.navigation.navigator import navigator
    navigator.stop()
    _autonomy_mode = "manual"
    _configure_vision_consumers(_autonomy_mode)
    if go2.is_connected:
        go2.stop_velocity()
    logging.warning("[Go2/Safety] emergency stop by %s (%s)", context.session_id, context.actor)
    return {"ok": True, "mode": "manual"}


@router.get("/control/lease")
async def go2_control_lease(context=Depends(require_authenticated_read)):
    lease = control_lease.snapshot()
    return asdict(lease) if lease is not None else None


@router.post("/control/lease/heartbeat")
async def go2_control_lease_heartbeat(context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    try:
        return asdict(control_lease.heartbeat(context))
    except LeaseConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "go2_lease_not_owned", "owner_session_id": exc.owner_session_id},
        ) from exc


@router.get("/navigation/state")
async def nav_state(context=Depends(require_authenticated_read)):
    from cloud.go2.navigation.navigator import navigator
    s = navigator.state
    return {**s, "mode": s["mode"].value if hasattr(s["mode"], "value") else s["mode"]}


@router.put("/led")
async def set_led(req: LedRequest, context: ExecutionContext = Depends(require_service_context)):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    try:
        await go2.set_led(req.color, req.duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "color": req.color, "duration": req.duration}


@router.get("/drive/state")
async def go2_drive_state(context=Depends(require_authenticated_read)):
    return drive.state_snapshot


@router.get("/memory")
async def go2_memory(context=Depends(require_admin_read)):
    """返回 Go2 的成长资产：调教记录 + 今天的事件流。"""
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.go2.agentcore.tools.tools import load_rules
    from cloud.go2.agentcore.memory.episode import episode_memory
    return {"taught": load_rules(), "today": episode_memory.format_today()}


@router.get("/autonomy")
async def go2_autonomy_get(context=Depends(require_authenticated_read)):
    return {"mode": _autonomy_mode}


@router.put("/autonomy")
async def go2_autonomy_set(req: AutonomyRequest, context: ExecutionContext = Depends(require_service_context)):
    global _autonomy_mode
    context = _normalize_context(context)
    _require_admin_persist(context)
    if req.mode not in ("manual", "reactive", "free_explore"):
        raise HTTPException(status_code=400, detail=f"未知自主模式: {req.mode}")
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if req.mode == _autonomy_mode:
        return {"ok": True, "mode": _autonomy_mode}

    if req.mode == "manual":
        logging.info("[Go2] 自主模式 → 完全遥控")
    elif req.mode == "reactive":
        logging.info("[Go2] 自主模式 → 自主反应")
    else:  # free_explore
        logging.info("[Go2] 自主模式 → 自由探索")

    _autonomy_mode = req.mode
    _configure_vision_consumers(_autonomy_mode, owner_session_id=context.session_id)
    return {"ok": True, "mode": _autonomy_mode}


# ── 身份端点 ──────────────────────────────────────────────────────

@router.get("/identity")
async def go2_identity_get(context=Depends(require_authenticated_read)):
    return asdict(identity_skill.current)


@router.post("/identity/recognize")
async def go2_identity_recognize(context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    with use_context(context):
        snapshot = await identity_skill.recognize_now()
    return asdict(snapshot)


@router.get("/people")
async def go2_people_list(context=Depends(require_admin_read)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    return [
        {"person_id": p.person_id, "name": p.name, "role": p.role, "face_count": p.face_count}
        for p in identity_skill.list_people()
    ]


@router.post("/people")
async def go2_people_create(req: PersonCreateRequest, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    with use_context(context):
        snapshot = await identity_skill.remember_person(req.name, req.role)
    if snapshot.status in {"rejected", "disabled"}:
        raise HTTPException(status_code=409, detail=snapshot.error_code)
    if snapshot.status == "error":
        raise HTTPException(status_code=503, detail=snapshot.error_code)
    return asdict(snapshot)


@router.post("/people/{person_id}/faces")
async def go2_people_add_face(person_id: str, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    with use_context(context):
        snapshot = await identity_skill.add_person_face(person_id)
    if snapshot.status in {"rejected", "disabled"}:
        raise HTTPException(status_code=409, detail=snapshot.error_code)
    if snapshot.status == "error":
        raise HTTPException(status_code=503, detail=snapshot.error_code)
    return asdict(snapshot)


@router.delete("/people/{person_id}")
async def go2_people_delete(person_id: str, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    try:
        with use_context(context):
            await identity_skill.forget_person(person_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except FaceProviderError as exc:
        status = 409 if exc.code == "enrollment_disabled" else 503
        raise HTTPException(status_code=status, detail=exc.code)
    return {"ok": True, "person_id": person_id}


@router.get("/personality")
async def go2_personality_get(context=Depends(require_admin_read)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.go2.agentcore.soul import get_system_prompt
    return {"prompt": get_system_prompt()}


@router.post("/personality")
async def go2_personality_set(req: PersonalityRequest, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.go2.agentcore.soul import set_personality
    with use_context(context):
        set_personality(req.prompt)
    return {"ok": True, "prompt": req.prompt}


@router.get("/debug/voxel_map")
async def go2_debug_voxel_map(context=Depends(require_admin_read)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    """返回最近一帧体素地图原始消息，用于格式调研。"""
    import json

    raw = go2.voxel_raw
    if raw is None:
        raise HTTPException(status_code=503, detail="尚未收到体素地图数据，请确认 Go2 已连接")

    def _safe(obj, depth=0):
        if depth > 6:
            return str(obj)
        if isinstance(obj, dict):
            return {k: _safe(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            preview = obj[:20]
            result = [_safe(x, depth + 1) for x in preview]
            if len(obj) > 20:
                result.append(f"... ({len(obj)} total)")
            return result
        if isinstance(obj, bytes):
            return {"__bytes_len__": len(obj), "__hex_preview__": obj[:32].hex()}
        try:
            json.dumps(obj)
            return obj
        except Exception:
            return str(obj)

    return _safe(raw)


@router.put("/navigation/home")
async def nav_set_home(context: ExecutionContext = Depends(require_service_context)):
    """把当前位置重新标记为 home。"""
    context = _normalize_context(context)
    _require_admin_persist(context)
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    odom = go2.odom
    if not odom:
        raise HTTPException(status_code=503, detail="暂无 Odom 数据")
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    with use_context(context):
        result = spatial_memory.tag_location("home", odom)
    return {"ok": True, "message": result}


@router.post("/navigation/home/go")
async def nav_go_home(context: ExecutionContext = Depends(require_service_context)):
    """归位：导航回 home 点。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    _acquire_motion(context)
    from cloud.go2.navigation.navigator import navigator
    task = asyncio.create_task(navigator.go_to("home", owner_session_id=context.session_id))
    session_tasks.track(context.session_id, task)
    return {"ok": True, "message": "开始归位"}


@router.post("/sessions/{session_id}/cleanup")
async def cleanup_go2_session(
    session_id: str,
    context: ExecutionContext = Depends(require_service_context),
):
    """Release only resources owned by one expired/logged-out session."""
    context = _normalize_context(context)
    if context.actor != "admin" and context.session_id != session_id:
        raise HTTPException(status_code=403, detail="cannot clean another session")
    return cleanup_session_resources(session_id)


def cleanup_session_resources(session_id: str) -> dict:
    """Idempotently release only resources owned by one trusted session."""
    from cloud.go2.agent import cleanup_session
    from cloud.go2.navigation.navigator import navigator

    lease = control_lease.snapshot()
    owns_motion = lease is not None and lease.owner_session_id == session_id
    history_removed = cleanup_session(session_id)
    tasks_cancelled = session_tasks.cancel(session_id)
    navigation_stopped = navigator.stop(owner_session_id=session_id)
    drive_stopped = drive.stop(owner_session_id=session_id)
    motion_stopped = False
    if owns_motion and go2.is_connected:
        try:
            # Cancellation of a timed chat move also reaches its StopMove
            # finally block.  Send a synchronous zero-velocity packet here as
            # the immediate safety boundary, without waiting for task teardown.
            go2.stop_velocity()
            motion_stopped = True
        except Exception:
            logging.warning("[Go2/Safety] failed to stop motion for session %s", session_id)
    lease_released = control_lease.release(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "history_removed": history_removed,
        "tasks_cancelled": tasks_cancelled,
        "navigation_stopped": navigation_stopped,
        "drive_stopped": drive_stopped,
        "motion_stopped": motion_stopped,
        "lease_released": lease_released,
    }


@router.get("/map/debug")
async def go2_map_debug(context=Depends(require_admin_read)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    """返回原始体素消息的结构，用于诊断数据格式。"""
    raw = go2.voxel_raw
    if raw is None:
        return {"error": "no voxel data yet"}

    def _summarize(obj, depth=0):
        if depth > 4:
            return "..."
        if isinstance(obj, dict):
            return {k: _summarize(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            length = len(obj)
            if length == 0:
                return []
            sample = obj[:6]
            return {"__len__": length, "__sample__": [_summarize(x, depth + 1) for x in sample]}
        return obj

    return _summarize(raw)


@router.get("/map.png")
async def go2_map_image(context=Depends(require_authenticated_read)):
    import io, math as _math
    import numpy as np
    from PIL import Image, ImageDraw
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    from cloud.go2.navigation.navigator import navigator

    SCALE = 3

    grid_obj = go2.occupancy_grid
    odom     = go2.odom

    if grid_obj is None:
        img = Image.new("RGB", (384, 384), (12, 14, 20))
        d   = ImageDraw.Draw(img)
        d.text((140, 188), "NO MAP", fill=(50, 60, 50))
        buf = io.BytesIO(); img.save(buf, "PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    nx, ny = grid_obj.width[0], grid_obj.width[1]
    W, H   = nx * SCALE, ny * SCALE

    # 翻转行让 Y 轴朝上（北朝上）与世界坐标一致
    arr = np.where(
        grid_obj.grid[::-1, :, None],
        np.array([70, 70, 70], dtype=np.uint8),   # 障碍：灰
        np.array([14, 18, 28], dtype=np.uint8),   # 自由：深蓝黑
    ).astype(np.uint8)
    img = Image.fromarray(arr, "RGB").resize((W, H), Image.NEAREST)
    d   = ImageDraw.Draw(img)

    def g2i(ix, iy):
        """grid cell → image pixel，Y 翻转后北朝上"""
        return ix * SCALE + SCALE // 2, (ny - 1 - iy) * SCALE + SCALE // 2

    # 机器狗内置导航规划的全局路径（蓝线）
    global_path = go2.global_path
    if len(global_path) > 1:
        pts = []
        for wp in global_path:
            gx, gy = grid_obj.odom_to_grid(wp["x"], wp["y"])
            pts.append(g2i(gx, gy))
        d.line(pts, fill=(30, 120, 255), width=2)

    # 保存的地点
    for loc in spatial_memory.list_locations():
        lx, ly = grid_obj.odom_to_grid(loc["x"], loc["y"])
        px, py = g2i(lx, ly)
        r = 5
        color  = (255, 200, 0) if navigator.target == loc["name"] else (160, 200, 80)
        d.ellipse([px-r, py-r, px+r, py+r], fill=color)
        d.text((px + r + 2, py - 5), loc["name"], fill=color)

    # 机器人（绿圆 + 朝向箭头）
    if odom:
        rix, riy = grid_obj.odom_to_grid(odom["x"], odom["y"])
    else:
        rix, riy = nx // 2, ny // 2
    px, py = g2i(rix, riy)
    r = 7
    d.ellipse([px-r, py-r, px+r, py+r], fill=(0, 220, 100), outline=(0, 255, 80), width=2)
    if odom:
        heading = odom.get("heading", 0.0)
        L = 14
        ax = px + int(_math.cos(heading) * L)
        ay = py - int(_math.sin(heading) * L)
        d.line([px, py, ax, ay], fill=(0, 255, 80), width=3)

    # 调试信息叠加
    obs_total = int(grid_obj.grid.sum())
    obs_pct   = obs_total * 100 // (nx * ny)
    if odom:
        d.text((4, H - 14),
               f"x={odom['x']:.2f} y={odom['y']:.2f} h={_math.degrees(odom['heading']):.0f}°",
               fill=(0, 180, 80))
    d.text((4, 4),
           f"orig=({grid_obj.origin[2]:.2f}z) res={grid_obj.resolution} "
           f"Z={grid_obj.origin[2]:.2f}~{grid_obj.origin[2]+grid_obj.width[2]*grid_obj.resolution:.2f}m",
           fill=(80, 80, 100))
    d.text((4, 18),
           f"obs={obs_total}/{nx*ny} ({obs_pct}%)",
           fill=(180, 80, 80))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(content=buf.getvalue(), media_type="image/png")
