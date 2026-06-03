import asyncio
import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import logging

from cloud.go2.connection import go2
from cloud.go2.vision import vision_loop

router = APIRouter()

# 当前注册的视觉规则回调，重连时先移除再重注册，防止重复触发
_active_rule_cb = None


@router.post("/api/go2/connect")
async def go2_connect():
    email    = os.getenv("GO2_EMAIL", "")
    password = os.getenv("GO2_PASSWORD", "")
    serial   = os.getenv("GO2_SERIAL", "")
    region   = os.getenv("GO2_REGION", "cn")
    if not email or not password or not serial:
        raise HTTPException(status_code=500, detail="GO2_EMAIL/PASSWORD/SERIAL 未配置")
    go2._last_error = None

    def _on_connect_done(t: asyncio.Task) -> None:
        global _active_rule_cb
        if not t.cancelled() and t.exception():
            go2._last_error = str(t.exception())
            return

        from cloud.go2.tools import check_rules, go2_sport

        # 移除上一次连接遗留的回调
        if _active_rule_cb is not None:
            vision_loop.remove_callback(_active_rule_cb)

        def _rule_cb(result) -> None:
            """OpenCV 检测结果 → 关键词 → 规则引擎 → 动作"""
            parts = []
            if result["persons"]["detected"]:
                parts.append("人")
            if result["faces"]["detected"]:
                parts.append("脸")
            if not parts:
                return
            observation = "，".join(parts)
            triggered = check_rules(observation)
            if not triggered:
                return
            try:
                loop = asyncio.get_running_loop()
                for action in triggered:
                    loop.create_task(go2_sport(action))
                    logging.info("[Go2Rules] vision→rule fired: %s", action)
            except RuntimeError:
                pass

        _active_rule_cb = _rule_cb
        vision_loop.start(lambda: go2._latest_frame)
        vision_loop.add_callback(_rule_cb)

    task = asyncio.create_task(go2.connect(email, password, serial, region))
    task.add_done_callback(_on_connect_done)
    return {"status": "connecting"}


@router.post("/api/go2/disconnect")
async def go2_disconnect():
    vision_loop.stop()
    await go2.disconnect()
    return {"status": "disconnected"}


@router.get("/api/go2/status")
def go2_status():
    return {
        "connected": go2.is_connected,
        "state": go2._robot_state,
        "error": getattr(go2, "_last_error", None),
    }


@router.get("/api/go2/video")
async def go2_video():
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 not connected")
    return StreamingResponse(
        go2.mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/api/go2/state")
async def go2_state():
    async def sse_gen():
        q = go2.new_state_queue()
        try:
            while go2.is_connected:
                try:
                    state = await asyncio.wait_for(q.get(), timeout=5.0)
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {}\n\n"
        finally:
            go2.remove_state_queue(q)

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


class CommandRequest(BaseModel):
    cmd: str
    params: dict = {}


class ModeRequest(BaseModel):
    mode: str


@router.post("/api/go2/command")
async def go2_command(req: CommandRequest):
    try:
        await go2.send_command(req.cmd, req.params or None)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@router.post("/api/go2/mode")
async def go2_mode(req: ModeRequest):
    if req.mode not in ("normal", "ai", "mcf"):
        raise HTTPException(status_code=400, detail=f"Unknown mode: {req.mode}")
    try:
        await go2.switch_mode(req.mode)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True, "mode": req.mode}


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str


@router.post("/api/go2/chat")
async def go2_chat(req: ChatRequest):
    from cloud.go2.agent import run_agent
    return await run_agent(req.session_id, req.message)


@router.get("/api/go2/vision/latest")
def go2_vision_latest():
    """返回最近一次 OpenCV 检测的结构化结果。"""
    if vision_loop.latest is None:
        raise HTTPException(status_code=503, detail="暂无检测数据，请确认 Go2 已连接")
    return vision_loop.latest


@router.get("/api/go2/vision/stream")
async def go2_vision_stream():
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
