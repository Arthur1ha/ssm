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
from cloud.go2.mode.free_explore import drive
from cloud.go2.perception.vision import vision_loop
from cloud.go2.perception.identity import identity_skill
from cloud.go2.control import LeaseConflict, control_lease, session_tasks
from cloud.go2.execution import (
    ExecutionContext, SYSTEM_CONTEXT, current_context, require_admin_read,
    require_authenticated_read, require_service_context, use_context,
)
from cloud.go2.card import (
    GO2_CARD_TOPIC, GO2_STATUS_TOPIC, _build_go2_card, _publish_go2_card,
    _publish_go2_status, _publish_go2_state, _clear_go2_card,
)

from cloud.go2.service import (
    prepare_user_interaction, perform_life_control, get_autonomy_mode,
    get_activity_mode_owner, set_activity_mode, clear_activity_mode_owner,
    _configure_vision_consumers, _initialize_identity_vision,
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

_connect_task: asyncio.Task | None = None
go2._autonomy_resume_callback = drive.resume_after_user


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
    """Navigation shares the same posture, ownership and lease preflight."""
    _check_direct_motion("Move", {}, context)


def _check_direct_motion(command: str, params: dict, context: ExecutionContext) -> None:
    from cloud.go2.tools.executor import check_direct_motion
    try:
        check_direct_motion(command, params, context)
    except LeaseConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "go2_control_busy", "owner_session_id": exc.owner_session_id,
            "expires_at": exc.expires_at,
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
        global _connect_task
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
        clear_activity_mode_owner()
        if identity_skill.enabled:
            asyncio.create_task(_initialize_identity_vision())
        else:
            _configure_vision_consumers("manual")

        # Connection is operational state, not permission to overwrite Home.

    _publish_go2_status()
    _connect_task = asyncio.create_task(go2.connect(email, password, serial, region))
    _connect_task.add_done_callback(_on_connect_done)
    return {"status": "connecting"}


@router.delete("/connection")
async def go2_disconnect(context: ExecutionContext = Depends(require_intervening_service_context)):
    global _connect_task
    if _connect_task is not None and not _connect_task.done():
        _connect_task.cancel()
        await asyncio.gather(_connect_task, return_exceptions=True)
    _connect_task = None
    from cloud.peers.gateway import cancel_pending
    cancel_pending(initiator_unit_id="go2", reason="mode_changed")
    await go2.disconnect()
    _configure_vision_consumers("manual")
    clear_activity_mode_owner()
    _publish_go2_card()
    _publish_go2_state()
    _publish_go2_status()
    return {"status": "disconnected"}


@router.get("/connection")
async def go2_status(context=Depends(require_authenticated_read)):
    return {**go2.state_snapshot(), "state": go2._robot_state,
            "error": getattr(go2, "_last_error", None)}


@router.get("/diagnostics/avoidance")
async def go2_avoidance_switch(context=Depends(require_admin_read)):
    """Inspect SWITCH_GET on the current connection without enabling movement."""
    try:
        return await go2.read_avoidance_switch()
    except (RuntimeError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="avoidance switch status unavailable") from exc


class NavigationRecordingRequest(BaseModel):
    duration_s: float = 10.0


_navigation_recording_active = False


@router.post("/diagnostics/navigation-recording")
async def navigation_recording(req: NavigationRecordingRequest,
                               context: ExecutionContext = Depends(require_service_context)):
    """Record observations only; this endpoint never starts or stops movement."""
    from cloud.go2.connection.recording import record_navigation
    global _navigation_recording_active
    if context.actor != "admin" or not context.persist:
        raise HTTPException(status_code=403, detail="administrator persistence required")
    if not 0 < req.duration_s <= 60 or context.expires_at < time.time() + req.duration_s:
        raise HTTPException(status_code=400, detail="invalid recording duration or expiry")
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 not connected")
    if _navigation_recording_active:
        raise HTTPException(status_code=409, detail="recording already active")
    _navigation_recording_active = True
    try:
        return await record_navigation(go2, req.duration_s)
    finally:
        _navigation_recording_active = False


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
                payload = {**go2.state_snapshot(), "state": go2._robot_state,
                           "error": getattr(go2, "_last_error", None)}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            go2.remove_state_queue(q)

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


class LifeRequest(BaseModel):
    action: str


@router.post('/life')
async def go2_life(req: LifeRequest, context: ExecutionContext = Depends(require_service_context)):
    from cloud.go2.tools.executor import execute_tool
    return await execute_tool('go2_life', {'action': req.action}, context=_normalize_context(context))


class CommandRequest(BaseModel):
    cmd: str = ""
    action: str = ""   # 与 cmd 等价，通用 FSM 派发字段；两者取非空者
    params: dict = {}
    request_id: str | None = None
    trace_id: str | None = None
    deadline: float | None = None


class VelocityRequest(BaseModel):
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0


@router.post("/commands")
async def go2_command(req: CommandRequest, context: ExecutionContext = Depends(require_service_context)):
    from cloud.go2.tools.executor import execute_command_request
    from cloud.go2.actions import SPORT_COMMANDS
    cmd = req.action or req.cmd
    if not cmd:
        raise HTTPException(status_code=400, detail="cmd or action required")
    context = _normalize_context(context)
    try:
        result = await execute_command_request(cmd, req.params, context=context,
                                              request_id=req.request_id, trace_id=req.trace_id,
                                              deadline=req.deadline,
                                              dispatch_only=(context.origin_kind == 'user_delegated'
                                                             and cmd in SPORT_COMMANDS and cmd != 'StopMove'))
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc) == "effect_call_id_conflict" else 400,
                            detail=str(exc)) from exc
    if result.get("result") == "blocked":
        status = (409 if result.get("error") == "control_lease_conflict" else
                  503 if result.get("message") == "Go2 未连接" else 400)
        detail = ({"code": "go2_control_busy", "message": "机器狗正在被其他体验者使用",
                   "owner_session_id": result.get("owner_session_id")} if status == 409 else
                  result.get("message") or result.get("error"))
        raise HTTPException(status_code=status, detail=detail, headers={
            "X-SSM-Command-Request-Id": result["command_request_id"],
            "X-SSM-Trace-Id": result["trace_id"],
        })
    if result.get("result") == "error":
        raise HTTPException(status_code=503, detail=result.get("message") or result.get("error"))
    return result


@router.get("/commands/{request_id}")
async def go2_command_result(request_id: str, context: ExecutionContext = Depends(require_service_context)):
    from cloud.go2.tasks import read_command_result
    context = _normalize_context(context)
    try:
        result = read_command_result(context, request_id, robot=go2)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="command_not_found")
    return result


@router.post("/velocity")
async def go2_velocity(req: VelocityRequest, context: ExecutionContext = Depends(require_service_context)):
    """WIRELESS_CONTROLLER 速度控制，无 ack，适合摇杆实时输入。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    command = "Move" if any((req.vx, req.vy, req.vyaw)) else "StopMove"
    _check_direct_motion(command, {"x": req.vx, "y": req.vy, "z": req.vyaw}
                         if command == "Move" else {}, context)
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
    if lease is not None and lease.owner_session_id != context.session_id:
        return {"result": "accepted", "error": "control_lease_transferred",
                "owner_session_id": lease.owner_session_id}
    from cloud.go2.navigation.navigator import navigator
    try:
        result = await navigator.stop_and_confirm(owner_session_id=context.session_id)
    except Exception as exc:
        logging.warning("[Go2/Task] stop confirmation failed: %s", type(exc).__name__)
        return {"result": "accepted", "error": "stop_unconfirmed"}
    return result if isinstance(result, dict) else {"result": "accepted", "error": "stop_unconfirmed"}


@router.post("/chat")
async def go2_chat(req: ChatRequest, context: ExecutionContext = Depends(require_service_context)):
    from cloud.agents.go2.graph import run_go2_loop
    context = _normalize_context(context)
    if req.trace_id is not None and not re.fullmatch(r"tr_[A-Fa-f0-9]{32}", req.trace_id):
        raise HTTPException(status_code=400, detail="invalid trace_id")
    if req.parent_run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", req.parent_run_id):
        raise HTTPException(status_code=400, detail="invalid parent_run_id")
    trace_id = req.trace_id or "tr_" + uuid.uuid4().hex
    if req.task_id is not None:
        from cloud.go2.tasks import chat_tasks
        if not req.task_id or len(req.task_id) > 128:
            raise HTTPException(status_code=400, detail="invalid task_id")
        from cloud.agents.go2.workflows.search import SEARCH_SERVER_TIMEOUT_S
        default_timeout = SEARCH_SERVER_TIMEOUT_S if req.skill_id == "go2_find_object" else 180 if req.skill_id == "go2_visual_navigate" else 45
        deadline = req.deadline if req.deadline is not None else time.time() + default_timeout
        if not isinstance(deadline, (int, float)) or not time.time() < deadline <= time.time() + 300:
            raise HTTPException(status_code=400, detail="invalid deadline")
        if not 0 <= req.sync_window_s <= 30:
            raise HTTPException(status_code=400, detail="invalid sync_window_s")
        fingerprint = hashlib.sha256(json.dumps({
            "message": req.message, "skill_id": req.skill_id, "params": req.params,
        }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

        async def run_task():
            return await run_go2_loop(
                context.session_id, req.message, skill_id=req.skill_id,
                params=req.params, context=context, run_id=req.task_id,
                deadline=deadline, trace_id=trace_id,
                parent_run_id=req.parent_run_id,
            )

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
    task = asyncio.create_task(
        run_go2_loop(
            context.session_id, req.message, skill_id=req.skill_id,
            params=req.params, context=context,
            trace_id=trace_id, parent_run_id=req.parent_run_id,
        ),
        name=f"go2-chat-{context.session_id}",
    )
    session_tasks.track(context.session_id, task)
    return await task


@router.get("/tasks/{task_id}")
async def go2_task_status(task_id: str, context: ExecutionContext = Depends(require_service_context)):
    from cloud.go2.tasks import chat_tasks
    from cloud.agents.go2.graph import get_store, _result
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
                          context: ExecutionContext = Depends(require_service_context)):
    from cloud.agents.go2.graph import run_go2_loop
    context = _normalize_context(context)
    return await run_go2_loop(context.session_id, req.message, context=context,
                              run_id=task_id, resume=True)


@router.post("/tasks/{task_id}/cancel")
async def go2_task_cancel(task_id: str, context: ExecutionContext = Depends(require_intervening_service_context)):
    from cloud.go2.tasks import chat_tasks
    context = _normalize_context(context)
    record = chat_tasks.get(task_id, context.session_id)
    if record is None:
        from cloud.agents.go2.graph import get_store, _result

        view = get_store()
        async with view.index.async_session_lock("go2", context.session_id):
            run = view.get(task_id, context.session_id)
            if run is None:
                raise HTTPException(status_code=404, detail="task not found")
            if run.status in {"executing", "needs_clarification"}:
                recovered = await view.reconcile_interrupted(context.session_id, cancelled=run)
                if recovered is None:
                    # An older non-head checkpoint cannot cancel a newer run.
                    return {**_result(run), "result": "unknown", "task_id": task_id,
                            "error": "historical_run_unconfirmed"}
                run = recovered
            return {**_result(run), "task_id": task_id}

    return await chat_tasks.cancel(record, lambda: _stop_task_motion(context))


@router.post("/chat/stream")
async def go2_chat_stream(req: ChatRequest, context: ExecutionContext = Depends(require_service_context)):
    """SSE 流：逐步推送规划、工具执行、最终回复事件。"""
    from cloud.agents.go2.graph import run_go2_loop

    context = _normalize_context(context)

    async def sse_gen():
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

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/mind/last_decision")
async def go2_mind_last_decision(context=Depends(require_authenticated_read)):
    """返回统一自主流程最近一次任务决策。"""
    d = drive.last_decision
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
    mode: str  # "manual" | "free_explore"


class PersonalityRequest(BaseModel):
    prompt: str
    social_initiative: int | None = None


@router.post("/navigation/locations")
async def nav_tag_location(req: TagLocationRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    odom = go2.odom
    if (not odom or go2.odom_received_at is None or
            time.monotonic() - go2.odom_received_at > 1.0 or
            go2.odom_frame_id != "odom"):
        raise HTTPException(status_code=503, detail="暂无 Odom 数据")
    from cloud.agents.go2.memory import spatial as spatial_memory
    with use_context(context):
        result = spatial_memory.tag_location(req.name, odom)
    return {"ok": True, "message": result}


@router.get("/navigation/locations")
async def nav_list_locations(context=Depends(require_authenticated_read)):
    from cloud.agents.go2.memory import spatial as spatial_memory
    return spatial_memory.list_locations()


@router.delete("/navigation/locations/{name}")
async def nav_delete_location(name: str, context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.agents.go2.memory import spatial as spatial_memory
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
    try:
        await prepare_user_interaction(context)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _acquire_motion(context)
    from cloud.go2.navigation.navigator import navigator
    task_id = navigator.start_go_to(req.name, owner_session_id=context.session_id)
    if navigator._task is not None:
        session_tasks.track(context.session_id, navigator._task)
    return {"ok": True, "task_id": task_id, "status": "accepted", "message": f"已受理导航到「{req.name}」"}


@router.post("/navigation/patrol")
async def nav_start_patrol(req: PatrolRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if not req.stops:
        raise HTTPException(status_code=400, detail="stops 不能为空")
    context = _normalize_context(context)
    try:
        await prepare_user_interaction(context)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _acquire_motion(context)
    from cloud.go2.navigation.navigator import navigator
    result = await navigator.start_patrol(req.stops, owner_session_id=context.session_id)
    return {"ok": False, "stops": req.stops, **result}


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
async def emergency_stop(context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    # The zero-velocity packet precedes activity teardown, navigation cleanup
    # and any RPC/observation waits. It remains available without posture data.
    if go2.is_connected:
        go2.stop_velocity()
    from cloud.peers.gateway import cancel_pending
    cancel_pending(origin_kind="agent_autonomous", reason="user_takeover")
    from cloud.go2.navigation.navigator import navigator
    navigator.stop()
    activity_owner = get_activity_mode_owner()
    _configure_vision_consumers("manual")
    if context.origin_kind == "user_delegated" and context.expires_at > time.time():
        from cloud.api.activity import user_intervention
        cleanup = asyncio.create_task(user_intervention(), name="go2-emergency-activity-cleanup")
        session_tasks.track(context.session_id, cleanup)
        def report_cleanup(done):
            # Keep the prior activity owner through teardown so its existing
            # mode adapter can release the corresponding software lease.
            if get_activity_mode_owner() == activity_owner:
                clear_activity_mode_owner()
            if not done.cancelled() and done.exception() is not None:
                logging.error("[Go2/Safety] activity cleanup failed: %s", done.exception())
        cleanup.add_done_callback(report_cleanup)
    else:
        clear_activity_mode_owner()
    logging.warning("[Go2/Safety] emergency stop requested by %s (%s)", context.session_id, context.actor)
    return {"ok": True, "result": "accepted", "mode": "manual", "physical_stop": "unknown"}


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


class SearchScopeRequest(BaseModel):
    session_id: str
    bounds: tuple[float, float, float, float] | None = None
    mode: str = "bounded"


@router.get("/navigation/search-scope")
async def search_scope_state(context=Depends(require_admin_read)):
    from cloud.agents.go2.workflows.search import configured_search_scope
    scope = configured_search_scope()
    session = getattr(go2, "_odom_session_id", None)
    return {"scope": scope, "session_id": session, "pose": go2.odom,
            "valid": bool(go2.is_connected and scope and scope.get("session_id") == session)}


@router.put("/navigation/search-scope")
async def set_search_scope(req: SearchScopeRequest,
                           context: ExecutionContext = Depends(require_service_context)):
    from cloud.agents.go2.workflows.search import configure_search_scope
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.state import runtime_for
    if context.actor != "admin" or context.origin_kind != "user_delegated":
        raise HTTPException(status_code=403, detail="administrator permission required")
    if context.expires_at <= time.time():
        raise HTTPException(status_code=403, detail="session expired")
    if not go2.is_connected or req.session_id != getattr(go2, "_odom_session_id", None):
        raise HTTPException(status_code=409, detail="search_scope_session_mismatch")
    runtime = runtime_for(go2)
    if (navigator.owner_session_id or go2.fsm_state == "moving"
            or runtime and runtime.data["life_state"] not in {"idle", "rest"}):
        raise HTTPException(status_code=409, detail="stop_active_task_before_configuring_scope")
    try:
        configure_search_scope(req.bounds, req.session_id, mode=req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"result": "ok", "session_id": req.session_id, "bounds": req.bounds, "mode": req.mode}


@router.get("/navigation/tasks/{task_id}")
async def nav_task_result(task_id: str, context=Depends(require_authenticated_read)):
    from cloud.go2.navigation.navigator import navigator
    result = navigator.task_result(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="导航任务不存在")
    return result


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
    """返回 Go2 的成长资产、今日事件及近期每日摘要。"""
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.agents.common import teachings
    from cloud.agents.go2.memory.episode import episode_memory
    from cloud.agents.go2.memory.daily_summary import get_recent_summaries
    from cloud.agents.go2.personality.profile import get_evolution_metadata
    return {"taught": teachings.list_for("go2", space_id=context.space_id),
            "today": episode_memory.format_today(),
            "summaries": get_recent_summaries(7),
            "last_evolved": get_evolution_metadata().get("last_evolved")}


@router.get("/autonomy")
async def go2_autonomy_get(context=Depends(require_authenticated_read)):
    return {"mode": get_autonomy_mode()}


@router.put("/autonomy")
async def go2_autonomy_set(req: AutonomyRequest, context: ExecutionContext = Depends(require_intervening_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    if req.mode not in ("manual", "free_explore"):
        raise HTTPException(status_code=400, detail=f"未知自主模式: {req.mode}")
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if req.mode == "manual":
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id="go2", origin_kind="agent_autonomous",
                       reason="mode_changed")
    if req.mode == get_autonomy_mode():
        clear_activity_mode_owner()
        return {"ok": True, "mode": get_autonomy_mode()}

    if req.mode == "manual":
        logging.info("[Go2] 自主模式 → 完全遥控")
    else:  # free_explore
        logging.info("[Go2] 自主模式 → 自由探索")

    clear_activity_mode_owner()
    _configure_vision_consumers(req.mode, owner_session_id=context.session_id, context=context)
    return {"ok": True, "mode": get_autonomy_mode()}


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
    from cloud.agents.go2.personality.profile import get_profile
    profile = get_profile()
    return {"prompt": profile["prompt"],
            "social_initiative": profile["traits"]["extraversion"]}


@router.post("/personality")
async def go2_personality_set(req: PersonalityRequest, context: ExecutionContext = Depends(require_service_context)):
    context = _normalize_context(context)
    _require_admin_persist(context)
    from cloud.agents.go2.personality.profile import get_profile, update_profile
    from cloud.personality.store import RevisionConflict
    from cloud.observability import emit_agent_event
    current = get_profile()
    changes = {"revision": current["revision"], "prompt": req.prompt}
    if req.social_initiative is not None:
        changes["traits"] = {"extraversion": req.social_initiative}
    try:
        updated = update_profile(changes, context)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    emit_agent_event("persona.changed", agent_id="go2", operator_kind="admin",
                     before_hash=hashlib.sha256(current["prompt"].encode()).hexdigest(),
                     after_hash=hashlib.sha256(updated["prompt"].encode()).hexdigest())
    return {"ok": True, "prompt": updated["prompt"],
            "social_initiative": updated["traits"]["extraversion"]}


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
    from cloud.agents.go2.memory import spatial as spatial_memory
    with use_context(context):
        result = spatial_memory.tag_location("home", odom)
    return {"ok": True, "message": result}


@router.post("/navigation/home/go")
async def nav_go_home(context: ExecutionContext = Depends(require_intervening_service_context)):
    """归位：导航回 home 点。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    context = _normalize_context(context)
    try:
        await prepare_user_interaction(context)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    from cloud.go2.navigation.navigator import navigator

    lease = control_lease.snapshot()
    owns_motion = lease is not None and lease.owner_session_id == session_id
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

    import numpy as np

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
        if isinstance(obj, np.ndarray):
            return {"__shape__": list(obj.shape), "__dtype__": str(obj.dtype),
                    "__sample__": _summarize(obj.reshape(-1)[:6].tolist(), depth + 1)}
        if isinstance(obj, np.generic):
            return obj.item()
        return obj

    # Capture immutable map/pose and freshness together, before expensive work.
    grid_obj = go2.occupancy_grid
    odom = go2.odom
    diagnostics = _summarize(go2.sensor_diagnostics)

    def build_grid_summary():
        try:
            from cloud.go2.navigation.occupancy_grid import PATH_CLEARANCE_M, TURN_CLEARANCE_M
            grid_summary = ({"floor_z": grid_obj.measured_floor_z,
                             "has_free_space_evidence": grid_obj.has_free_space_evidence,
                             "free_cells": int(np.count_nonzero(grid_obj.cells == 1)),
                             "unknown_cells": int(np.count_nonzero(grid_obj.cells == 0)),
                             "occupied_cells": int(np.count_nonzero(grid_obj.cells == 2)),
                             "raw_obstacle_cells": int(np.count_nonzero(grid_obj.obstacles)),
                             "inflated_only_cells": int(np.count_nonzero(
                                 (grid_obj.cells == 2) & ~grid_obj.obstacles)),
                             "path_clearance_m": PATH_CLEARANCE_M,
                             "turn_clearance_m": TURN_CLEARANCE_M}
                            if grid_obj is not None else None)
            if grid_summary is not None and odom and grid_obj.has_free_space_evidence:
                from cloud.go2.navigation.frontier import (
                    find_frontier_target, raycast_directions, reachable_summary,
                )
                grid_summary["reachable_directions_m"] = raycast_directions(grid_obj, odom)
                grid_summary.update(reachable_summary(grid_obj, odom))
                grid_summary["frontier_target"] = find_frontier_target(grid_obj, odom)
                grid_summary["body_clear"] = grid_obj.is_body_clear(
                    odom["x"], odom["y"], odom.get("heading", 0.0))
        except (ValueError, TypeError):
            grid_summary = None
        return grid_summary

    from cloud.agents.common.runner import invoke_blocking
    grid_summary = await invoke_blocking(build_grid_summary)
    return {"diagnostics": diagnostics, "voxel": _summarize(raw), "grid": grid_summary}


@router.get("/diagnostics/rgb-lidar/capture")
async def capture_rgb_lidar_target(target: str, context=Depends(require_admin_read)):
    """Capture a named target and its paired 3D voxel sample without moving Go2."""
    from cloud.observability import bind_context
    from cloud.go2.perception.rgb_lidar import save_paired_sample
    from cloud.go2.perception.visual_target import save_diagnostic_frame
    from cloud.go2.perception.visual_target import detect_visible_target

    if not target.strip() or len(target) > 100:
        raise HTTPException(status_code=400, detail="请提供画面中的目标描述")
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    captured = go2.latest_frame_snapshot()
    voxel = go2.voxel_raw
    voxel_received_at = go2.voxel_received_at
    if captured is None or voxel is None:
        raise HTTPException(status_code=503, detail="视频或雷达数据尚未就绪")
    jpeg, frame = captured
    session_id = (context.session_id if isinstance(context, ExecutionContext)
                  else context.get("session_id") if isinstance(context, dict) else None)
    with bind_context(session_id=session_id):
        image_path = save_diagnostic_frame(jpeg, frame)
        observation = await detect_visible_target(jpeg, target, frame)
        sample_path = save_paired_sample(image_path, frame, voxel, voxel_received_at,
                                         observation.get("bbox_1000"))
    return {"target": target, "frame_id": frame.get("frame_id"),
            "observation": observation, "image_artifact": str(image_path) if image_path else None,
            "lidar_artifact": str(sample_path) if sample_path else None,
            "lidar_status": "paired" if sample_path else "unpaired",
            "voxel_skew_s": round(voxel_received_at - frame["received_at"], 3)
            if isinstance(voxel_received_at, (int, float)) else None,
            "motion_command_sent": False}


@router.get("/map.png")
async def go2_map_image(context=Depends(require_authenticated_read)):
    import io, math as _math
    import numpy as np
    from PIL import Image, ImageDraw
    from cloud.agents.go2.memory import spatial as spatial_memory
    from cloud.go2.navigation.navigator import navigator

    if (not go2.is_connected or go2.voxel_received_at is None
            or time.monotonic() - go2.voxel_received_at > 20.0):
        raise HTTPException(status_code=503, detail="地图数据尚未就绪")

    SCALE = 3

    grid_obj = go2.occupancy_grid
    odom     = go2.odom
    if (go2.odom_received_at is None or time.monotonic() - go2.odom_received_at > 1.0 or
            go2.odom_frame_id != grid_obj.frame_id):
        odom = {}

    if grid_obj is None:
        raise HTTPException(status_code=503, detail="地图数据尚未就绪")

    nx, ny = grid_obj.width[0], grid_obj.width[1]
    W, H   = nx * SCALE, ny * SCALE

    # 翻转行让 Y 轴朝上（北朝上）与世界坐标一致
    from cloud.go2.navigation.occupancy_grid import FREE, OCCUPIED
    arr = np.empty((ny, nx, 3), dtype=np.uint8)
    arr[:] = (14, 18, 28)  # unknown
    arr[grid_obj.cells == FREE] = (34, 65, 70)
    arr[grid_obj.cells == OCCUPIED] = (100, 100, 100)  # inflated clearance
    arr[grid_obj.obstacles] = (210, 85, 75)  # raw obstacle returns
    arr = arr[::-1]
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
            try:
                gx, gy = grid_obj.odom_to_grid(wp["x"], wp["y"])
                pts.append(g2i(gx, gy))
            except ValueError:
                continue
        if len(pts) > 1:
            d.line(pts, fill=(30, 120, 255), width=2)

    # 保存的地点
    for loc in spatial_memory.list_locations():
        if loc.get("session_id") != getattr(go2, "_odom_session_id", None):
            continue
        try:
            lx, ly = grid_obj.odom_to_grid(loc["x"], loc["y"])
        except ValueError:
            continue
        px, py = g2i(lx, ly)
        r = 5
        color  = (255, 200, 0) if navigator.target == loc["name"] else (160, 200, 80)
        d.ellipse([px-r, py-r, px+r, py+r], fill=color)
        d.text((px + r + 2, py - 5), loc["name"], fill=color)

    # 机器人（绿圆 + 朝向箭头）
    if odom:
        try:
            rix, riy = grid_obj.odom_to_grid(odom["x"], odom["y"])
        except ValueError:
            odom = {}
        else:
            px, py = g2i(rix, riy)
            r = 7
            d.ellipse([px-r, py-r, px+r, py+r], fill=(0, 220, 100), outline=(0, 255, 80), width=2)
            heading = odom["heading"]
            L = 14
            ax = px + int(_math.cos(heading) * L)
            ay = py - int(_math.sin(heading) * L)
            d.line([px, py, ax, ay], fill=(0, 255, 80), width=3)
            d.rectangle([4, 4, 126, 21], fill=(14, 18, 28))
            d.text((7, 7), f"x={odom['x']:.2f} y={odom['y']:.2f}",
                   fill=(190, 245, 205))

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "private, no-store"})
