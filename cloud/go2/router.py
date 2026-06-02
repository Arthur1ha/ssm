import asyncio
import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import logging

from cloud.go2.connection import go2

router = APIRouter()


@router.post("/api/go2/connect")
async def go2_connect():
    email    = os.getenv("GO2_EMAIL", "")
    password = os.getenv("GO2_PASSWORD", "")
    serial   = os.getenv("GO2_SERIAL", "")
    region   = os.getenv("GO2_REGION", "cn")
    if not email or not password or not serial:
        raise HTTPException(status_code=500, detail="GO2_EMAIL/PASSWORD/SERIAL 未配置")
    go2._last_error = None
    task = asyncio.create_task(go2.connect(email, password, serial, region))
    task.add_done_callback(lambda t: setattr(go2, "_last_error", str(t.exception())) if not t.cancelled() and t.exception() else None)
    return {"status": "connecting"}


@router.post("/api/go2/disconnect")
async def go2_disconnect():
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
