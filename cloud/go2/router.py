"""cloud.go2.router — Go2 机器狗 FastAPI 路由。

负责 Go2 连接/断开/控制的 HTTP 端点与自主模式装配。
Card 构建和 MQTT retained 发布由 card.py 实现。
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from cloud.go2.connection import go2
from cloud.go2.agentcore.mode.free_explore import drive
from cloud.go2.agentcore.mode.reactive import reactive_mind
from cloud.go2.agentcore.perception.vision import vision_loop
from cloud.go2.agentcore.perception.identity import IdentityEvent, identity_skill
from cloud.go2.agentcore.memory.episode import EventType, episode_memory
from cloud.go2.control import LeaseConflict, control_lease, session_tasks
from cloud.go2.execution import (
    ExecutionContext, SYSTEM_CONTEXT, current_context, require_admin_read,
    require_authenticated_read, require_service_context, use_context,
)
from cloud.go2.card import (
    GO2_CARD_TOPIC, GO2_STATUS_TOPIC, _build_go2_card, _publish_go2_card,
    _publish_go2_status, _publish_go2_state, _clear_go2_card,
)

# MQTT 客户端，由 api/main.py 通过 init_mqtt() 注入
_mqtt_client = None


def init_mqtt(client) -> None:
    """注入 api 进程的 MQTT 客户端，供发布/清除 Go2 card 使用。"""
    global _mqtt_client
    _mqtt_client = client
    from cloud.go2.card import init_mqtt as init_card_mqtt
    init_card_mqtt(client)


router = APIRouter(prefix="/api/go2", tags=["go2"])

# 当前注册的视觉回调句柄，重连时先移除再重注册，防止重复触发
_active_rule_cb  = None
_active_drive_cb = None
_active_identity_cb = None
_autonomy_mode   = "manual"  # "manual" | "reactive" | "free_explore"
_activity_mode_owner: str | None = None
_connect_task: asyncio.Task | None = None


def get_activity_mode_owner() -> str | None:
    return _activity_mode_owner


async def set_activity_mode(mode: str, activity_id: str) -> dict:
    """Switch Go2 autonomy for a server-validated space activity.

    This in-process path never accepts a model-supplied actor. Ownership is
    checked again on release so a later direct user choice is not overwritten.
    """
    global _autonomy_mode, _activity_mode_owner
    if not activity_id.startswith("act_") or mode not in {"manual", "free_explore"}:
        raise ValueError("invalid activity mode request")
    if mode == "manual":
        if _activity_mode_owner != activity_id:
            return {"ok": False, "mode": _autonomy_mode, "reason": "not_activity_owned"}
        _activity_mode_owner = None
        _autonomy_mode = "manual"
        try:
            _configure_vision_consumers("manual")
        except Exception:
            logging.exception("[Go2/Activity] failed to stop autonomous consumers")
            drive.user_interrupt = True
            try:
                drive.stop()
            except Exception:
                logging.exception("[Go2/Activity] drive stop failed")
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id="go2", origin_kind="agent_autonomous",
                       reason="activity_ended")
        if not go2.is_connected:
            control_lease.release(activity_id)
            return {"ok": True, "mode": "manual", "physical_stop": "unknown"}
        try:
            stop = await go2.send_command("StopMove", completion_timeout=5.0)
        except Exception as exc:
            logging.warning("[Go2/Activity] stop not confirmed: %s", exc)
            return {"ok": True, "mode": "manual", "physical_stop": "unknown"}
        if stop.get("result") == "ok":
            control_lease.release(activity_id)
        return {"ok": True, "mode": "manual",
                "physical_stop": "confirmed" if stop.get("result") == "ok" else "unknown"}
    if not go2.is_connected:
        return {"ok": False, "mode": _autonomy_mode, "reason": "offline"}
    if _autonomy_mode != "manual" and _activity_mode_owner != activity_id:
        return {"ok": False, "mode": _autonomy_mode, "reason": "mode_owned_elsewhere"}
    lease = control_lease.snapshot()
    if lease is not None and lease.owner_session_id != activity_id:
        return {"ok": False, "mode": _autonomy_mode, "reason": "motion_busy"}
    if _activity_mode_owner == activity_id and _autonomy_mode == mode:
        return {"ok": True, "mode": mode, "owner": activity_id}
    _autonomy_mode = mode
    _activity_mode_owner = activity_id
    activity_context = ExecutionContext(activity_id, "visitor", False,
                                        "main", "agent_autonomous", 0)
    try:
        _configure_vision_consumers(mode, owner_session_id=activity_id, context=activity_context)
    except Exception:
        _autonomy_mode = "manual"
        _activity_mode_owner = None
        try:
            _configure_vision_consumers("manual")
        except Exception:
            logging.exception("[Go2/Activity] failed to roll back autonomous consumers")
        raise
    return {"ok": _autonomy_mode == mode, "mode": _autonomy_mode, "owner": activity_id}

# Continuous identity vision is global device activity, not an administrator
# request. It must inherit a non-persistent context. Explicit admin-triggered
# recognition still uses that request's trusted context and may be remembered.
_IDENTITY_VISION_CONTEXT = ExecutionContext(
    "go2-identity-vision", "visitor", False,
)


def _normalize_context(context) -> ExecutionContext:
    """Direct Python calls are already trusted; HTTP always supplies a verified context."""
    return context if isinstance(context, ExecutionContext) else SYSTEM_CONTEXT


async def require_intervening_service_context(
    context: ExecutionContext = Depends(require_service_context),
) -> ExecutionContext:
    """A verified new user write takes priority over an ongoing activity."""
    if context.origin_kind == "user_delegated" and context.expires_at > time.time():
        from cloud.api.activity import user_intervention
        await user_intervention()
    return context


def _cancel_pending_peer_requests(context: ExecutionContext) -> None:
    """A direct user command takes priority over queued Go2 collaboration."""
    if context.origin_kind != "user_delegated" or context.session_id == "system":
        return
    from cloud.peers.gateway import cancel_pending
    cancel_pending(initiator_unit_id="go2", origin_kind="agent_autonomous",
                   reason="user_takeover")


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


def _check_direct_motion(command: str, params: dict, context: ExecutionContext) -> None:
    """Apply the tool motion preflight to FSM buttons and joystick input."""
    from cloud.go2.agentcore.tools.executor import check_direct_motion
    previous = control_lease.snapshot()
    if (command == "StopMove" and previous is not None
            and previous.owner_session_id != context.session_id):
        raise HTTPException(status_code=409, detail={
            "code": "go2_control_busy", "message": "当前会话不能停止其他会话的运动",
            "owner_session_id": previous.owner_session_id, "expires_at": previous.expires_at,
        })
    try:
        check_direct_motion(command, params, context)
    except LeaseConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "go2_control_busy", "message": "机器狗正在被其他体验者使用",
            "owner_session_id": exc.owner_session_id, "expires_at": exc.expires_at,
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (command != "StopMove" and previous is not None
            and previous.owner_session_id != context.session_id and context.actor == "admin"):
        from cloud.go2.navigation.navigator import navigator
        navigator.stop(owner_session_id=previous.owner_session_id)
        session_tasks.cancel(previous.owner_session_id)
        logging.info("[Go2/Lease] admin %s preempted %s", context.session_id, previous.owner_session_id)


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


def _configure_vision_consumers(mode: str, owner_session_id: str = "system", context: ExecutionContext | None = None) -> None:
    """统一重配视觉消费者；身份在三种 autonomy 模式下都可保持运行。"""
    global _active_rule_cb, _active_drive_cb, _active_identity_cb
    reactive_mind.set_active(False)
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
        reactive_mind.set_active(True, context)
        _active_rule_cb = reactive_mind.on_vision_frame
        vision_loop.add_callback(_active_rule_cb)
    elif mode == "free_explore":
        _active_drive_cb = drive.on_vision_frame
        vision_loop.add_callback(_active_drive_cb)
        drive.start(owner_session_id=owner_session_id, context=context)

    if any((_active_identity_cb, _active_rule_cb, _active_drive_cb)):
        vision_loop.start(lambda: go2._latest_frame)
    else:
        vision_loop.stop()


@router.post("/connection")
async def go2_connect(context: ExecutionContext = Depends(require_intervening_service_context)):
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
        global _autonomy_mode, _activity_mode_owner, _connect_task
        _connect_task = None
        if t.cancelled():
            _publish_go2_state()
            _publish_go2_status()
            return
        if t.exception():
            go2._last_error = str(t.exception())
            _publish_go2_card()
            _publish_go2_state()
            _publish_go2_status()
            return

        _publish_go2_card()
        _publish_go2_state()
        _publish_go2_status()

        # 默认完全遥控，但已启用的身份消费者仍保持视觉检测。
        _autonomy_mode = "manual"
        _activity_mode_owner = None
        if identity_skill.enabled:
            asyncio.create_task(_initialize_identity_vision())
        else:
            _configure_vision_consumers(_autonomy_mode)

        # Connection is operational state, not permission to overwrite Home.

    _publish_go2_status()
    _connect_task = asyncio.create_task(go2.connect(email, password, serial, region))
    _connect_task.add_done_callback(_on_connect_done)
    return {"status": "connecting"}


@router.delete("/connection")
async def go2_disconnect(context: ExecutionContext = Depends(require_intervening_service_context)):
    global _autonomy_mode, _activity_mode_owner
    from cloud.peers.gateway import cancel_pending
    cancel_pending(initiator_unit_id="go2", reason="mode_changed")
    await go2.disconnect()
    _configure_vision_consumers("manual")
    _autonomy_mode   = "manual"
    _activity_mode_owner = None
    _publish_go2_card()
    _publish_go2_state()
    _publish_go2_status()
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


@router.get("/activity-media/{activity_id}/{question_id}")
async def go2_activity_media(activity_id: str, question_id: str,
                             context=Depends(require_authenticated_read)):
    """Serve a captured question frame to an authenticated space member."""
    from cloud.go2.activity_media import load_snapshot
    frame = load_snapshot(activity_id, question_id)
    if frame is None:
        raise HTTPException(status_code=404, detail="image not found")
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


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
async def go2_command(req: CommandRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    cmd = req.action or req.cmd
    if not cmd:
        raise HTTPException(status_code=400, detail="cmd or action required")
    context = _normalize_context(context)
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    from unitree_webrtc_connect.constants import SPORT_CMD
    # EnterMoveControl only enters the local FSM; it has no SPORT_MOD command ID.
    if cmd != "EnterMoveControl" and cmd not in SPORT_CMD:
        raise HTTPException(status_code=400, detail=f"Unknown command: {cmd}")
    _check_direct_motion(cmd, req.params or {}, context)
    _cancel_pending_peer_requests(context)
    try:
        result = await go2.send_command(cmd, req.params or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return result


@router.post("/velocity")
async def go2_velocity(req: VelocityRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    """WIRELESS_CONTROLLER 速度控制，无 ack，适合摇杆实时输入。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    command = "Move" if any((req.vx, req.vy, req.vyaw)) else "StopMove"
    _check_direct_motion(command, {"x": req.vx, "y": req.vy, "z": req.vyaw}, context)
    _cancel_pending_peer_requests(context)
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
    task_id: str | None = None
    deadline: float | None = None
    sync_window_s: float = 8.0
    trace_id: str | None = None
    parent_run_id: str | None = None


async def _stop_task_motion(context: ExecutionContext) -> dict:
    """Stop only motion still owned by this task's trusted session."""
    lease = control_lease.snapshot()
    if lease is None:
        return {"result": "ok", "message": "no_active_motion_lease"}
    if lease.owner_session_id != context.session_id:
        return {"result": "accepted", "error": "control_lease_transferred",
                "owner_session_id": lease.owner_session_id}
    from cloud.go2.navigation.navigator import navigator
    try:
        navigator.stop(owner_session_id=context.session_id)
    except Exception as exc:
        logging.warning("[Go2/Task] navigator stop failed: %s", exc)
    if not go2.is_connected:
        return {"result": "accepted", "error": "device_disconnected"}
    try:
        result = await go2.send_command("StopMove")
    except Exception as exc:
        return {"result": "accepted", "error": str(exc)}
    return result if isinstance(result, dict) else {"result": "accepted", "error": "stop_unconfirmed"}


@router.post("/chat")
async def go2_chat(req: ChatRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    from cloud.go2.multitask import run_go2_loop
    context = _normalize_context(context)
    if req.trace_id is not None and not re.fullmatch(r"tr_[A-Fa-f0-9]{32}", req.trace_id):
        raise HTTPException(status_code=400, detail="invalid trace_id")
    if req.parent_run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", req.parent_run_id):
        raise HTTPException(status_code=400, detail="invalid parent_run_id")
    trace_id = req.trace_id or "tr_" + uuid.uuid4().hex
    _cancel_pending_peer_requests(context)
    if req.task_id is not None:
        from cloud.go2.tasks import chat_tasks
        if not req.task_id or len(req.task_id) > 128:
            raise HTTPException(status_code=400, detail="invalid task_id")
        deadline = req.deadline if req.deadline is not None else time.time() + 45
        if not isinstance(deadline, (int, float)) or not time.time() < deadline <= time.time() + 300:
            raise HTTPException(status_code=400, detail="invalid deadline")
        if not 0 <= req.sync_window_s <= 30:
            raise HTTPException(status_code=400, detail="invalid sync_window_s")
        fingerprint = hashlib.sha256(json.dumps({
            "message": req.message, "skill_id": req.skill_id, "params": req.params,
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        async def run_task():
            drive.user_interrupt = True
            try:
                return await run_go2_loop(
                    context.session_id, req.message, skill_id=req.skill_id,
                    params=req.params, context=context, run_id=req.task_id,
                    deadline=deadline, trace_id=trace_id,
                    parent_run_id=req.parent_run_id,
                )
            finally:
                drive.user_interrupt = False

        try:
            record = chat_tasks.start(
                req.task_id, context.session_id, fingerprint, deadline, run_task,
                on_timeout=lambda: _stop_task_motion(context),
                trace_id=trace_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session_tasks.track(context.session_id, record.task)
        result = await chat_tasks.wait(record, req.sync_window_s)
        return JSONResponse(result, status_code=202) if result.get("result") == "executing" else result
    drive.user_interrupt = True
    task = asyncio.create_task(
        run_go2_loop(
            context.session_id, req.message, skill_id=req.skill_id,
            params=req.params, context=context,
            trace_id=trace_id, parent_run_id=req.parent_run_id,
        ),
        name=f"go2-chat-{context.session_id}",
    )
    session_tasks.track(context.session_id, task)
    try:
        return await task
    finally:
        drive.user_interrupt = False


@router.get("/tasks/{task_id}")
async def go2_task_status(task_id: str, context: ExecutionContext = Depends(require_intervening_service_context)):
    from cloud.go2.tasks import chat_tasks
    from cloud.go2.multitask import get_store, _result
    context = _normalize_context(context)
    record = chat_tasks.get(task_id, context.session_id)
    if record is None:
        run = get_store().get(task_id, context.session_id)
        if run is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _result(run)
    result = record.snapshot()
    return JSONResponse(result, status_code=202) if result.get("result") == "executing" else result


@router.post("/tasks/{task_id}/resume")
async def go2_task_resume(task_id: str, req: ChatRequest,
                          context: ExecutionContext = Depends(require_intervening_service_context)):
    from cloud.go2.multitask import run_go2_loop
    context = _normalize_context(context)
    return await run_go2_loop(context.session_id, req.message, context=context,
                              run_id=task_id, resume=True)


@router.post("/tasks/{task_id}/cancel")
async def go2_task_cancel(task_id: str, context: ExecutionContext = Depends(require_intervening_service_context)):
    from cloud.go2.tasks import chat_tasks
    context = _normalize_context(context)
    record = chat_tasks.get(task_id, context.session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")

    return await chat_tasks.cancel(record, lambda: _stop_task_motion(context))


@router.post("/chat/stream")
async def go2_chat_stream(req: ChatRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    """SSE 流：逐步推送规划、工具执行、最终回复事件。"""
    from cloud.go2.multitask import run_go2_loop

    context = _normalize_context(context)
    _cancel_pending_peer_requests(context)

    async def sse_gen():
        drive.user_interrupt = True
        stream_task = asyncio.current_task()
        if stream_task is not None:
            session_tasks.track(context.session_id, stream_task)
        try:
            queue = asyncio.Queue()
            task = asyncio.create_task(run_go2_loop(
                context.session_id, req.message, context=context,
                run_id=req.task_id, deadline=req.deadline,
                trace_id=req.trace_id, parent_run_id=req.parent_run_id,
                on_step=lambda step: queue.put_nowait({"type": "tool_done", "tool": step["tool"],
                                                       "step_id": step["step_id"], "result": step["result"]}),
            ))
            yield f"data: {json.dumps({'type': 'thinking', 'text': '正在理解指令…'}, ensure_ascii=False)}\n\n"
            while not task.done() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            result = await task
            yield f"data: {json.dumps({'type': 'response', 'text': result.get('response', '')}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', **result}, ensure_ascii=False)}\n\n"
        finally:
            if 'task' in locals() and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
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


@router.post("/navigation/locations")
async def nav_tag_location(req: TagLocationRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
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
async def nav_delete_location(name: str, context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    with use_context(context):
        deleted = spatial_memory.delete_location(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"地点「{name}」不存在")
    return {"ok": True}


@router.post("/navigation/go")
async def nav_go(req: NavigateRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    _acquire_motion(context)
    from cloud.go2.navigation.navigator import navigator
    task = asyncio.create_task(navigator.go_to(req.name, owner_session_id=context.session_id))
    session_tasks.track(context.session_id, task)
    return {"ok": True, "message": f"开始导航到「{req.name}」"}


@router.post("/navigation/patrol")
async def nav_start_patrol(req: PatrolRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
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
async def nav_stop(context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    from cloud.go2.navigation.navigator import navigator
    stopped = navigator.stop(owner_session_id=context.session_id)
    if not stopped and context.actor == "admin":
        navigator.stop()
        stopped = True
    return {"ok": True, "stopped": stopped}


@router.post("/stop")
async def emergency_stop(context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    global _autonomy_mode, _activity_mode_owner
    from cloud.peers.gateway import cancel_pending
    cancel_pending(origin_kind="agent_autonomous", reason="user_takeover")
    from cloud.go2.navigation.navigator import navigator
    navigator.stop()
    _autonomy_mode = "manual"
    _activity_mode_owner = None
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
async def go2_control_lease_heartbeat(context: ExecutionContext = Depends(require_intervening_service_context)):
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
async def set_led(req: LedRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
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
async def go2_autonomy_set(req: AutonomyRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    global _autonomy_mode, _activity_mode_owner
    context = _normalize_context(context)
    _require_admin_persist(context)
    if req.mode not in ("manual", "reactive", "free_explore"):
        raise HTTPException(status_code=400, detail=f"未知自主模式: {req.mode}")
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if req.mode == "manual":
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id="go2", origin_kind="agent_autonomous",
                       reason="mode_changed")
    if req.mode == _autonomy_mode:
        _activity_mode_owner = None
        return {"ok": True, "mode": _autonomy_mode}

    if req.mode == "manual":
        logging.info("[Go2] 自主模式 → 完全遥控")
    elif req.mode == "reactive":
        logging.info("[Go2] 自主模式 → 自主反应")
    else:  # free_explore
        logging.info("[Go2] 自主模式 → 自由探索")

    _autonomy_mode = req.mode
    _activity_mode_owner = None
    _configure_vision_consumers(_autonomy_mode, owner_session_id=context.session_id, context=context)
    return {"ok": True, "mode": _autonomy_mode}


# ── 身份端点 ──────────────────────────────────────────────────────

@router.get("/identity")
async def go2_identity_get(context=Depends(require_authenticated_read)):
    return asdict(identity_skill.current)


@router.post("/identity/recognize")
async def go2_identity_recognize(context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    with use_context(context):
        snapshot = await identity_skill.recognize_now()
    return asdict(snapshot)


@router.get("/personality")
async def go2_personality_get(context=Depends(require_admin_read)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.go2.agentcore.soul import get_system_prompt
    return {"prompt": get_system_prompt()}


@router.post("/personality")
async def go2_personality_set(req: PersonalityRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
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
async def nav_set_home(context: ExecutionContext = Depends(require_intervening_service_context)):
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
async def nav_go_home(context: ExecutionContext = Depends(require_intervening_service_context)):
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
