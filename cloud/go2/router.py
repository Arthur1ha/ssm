import asyncio
import json
import logging
import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from cloud.go2.connection import go2
from cloud.go2.navigation.drive import drive
from cloud.go2.agentcore.skills.reactive import reactive_mind
from cloud.go2.agentcore.skills.vision import vision_loop

_DEVICES_FILE = Path(__file__).parent.parent / "orchestrator" / "devices.json"


def _register_go2(serial: str) -> None:
    try:
        devices = json.loads(_DEVICES_FILE.read_text()) if _DEVICES_FILE.exists() else {}
    except Exception:
        devices = {}
    devices["go2"] = {
        "unit_id":      "go2",
        "slug":         "go2",
        "name":         "Go2 机器狗",
        "agent_type":   "robot",
        "hw_platform":  "go2",
        "serial":       serial,
        "capabilities": ["sport", "move", "vision", "rules", "chat"],
        "ts":           int(time.time()),
    }
    _DEVICES_FILE.write_text(json.dumps(devices, ensure_ascii=False, indent=2))
    logging.info("[Go2] 已注册到 devices.json (slug=go2)")


def _unregister_go2() -> None:
    try:
        devices = json.loads(_DEVICES_FILE.read_text()) if _DEVICES_FILE.exists() else {}
    except Exception:
        return
    devices.pop("go2", None)
    _DEVICES_FILE.write_text(json.dumps(devices, ensure_ascii=False, indent=2))
    logging.info("[Go2] 已从 devices.json 移除")

router = APIRouter()

# 当前注册的视觉回调句柄，重连时先移除再重注册，防止重复触发
_active_rule_cb  = None
_active_drive_cb = None
_autonomy_mode   = "remote"  # "remote" | "free_explore"


@router.post("/api/go2/connection")
async def go2_connect():
    email    = os.getenv("GO2_EMAIL", "")
    password = os.getenv("GO2_PASSWORD", "")
    serial   = os.getenv("GO2_SERIAL", "")
    region   = os.getenv("GO2_REGION", "cn")
    if not email or not password or not serial:
        raise HTTPException(status_code=500, detail="GO2_EMAIL/PASSWORD/SERIAL 未配置")
    go2._last_error = None

    def _on_connect_done(t: asyncio.Task) -> None:
        global _active_rule_cb, _active_drive_cb, _autonomy_mode
        if not t.cancelled() and t.exception():
            go2._last_error = str(t.exception())
            return

        _register_go2(serial)

        if _active_rule_cb is not None:
            vision_loop.remove_callback(_active_rule_cb)
            _active_rule_cb = None
        if _active_drive_cb is not None:
            vision_loop.remove_callback(_active_drive_cb)
            _active_drive_cb = None

        # 默认完全遥控：不启动 VisionLoop，不注册任何回调
        _autonomy_mode = "manual"

        from cloud.go2.agentcore.memory import spatial as _sm
        _sm.tag_location("home", {"x": 0.0, "y": 0.0, "heading": 0.0})
        logging.info("[Go2] 已将起点标记为 home (0, 0, 0)")

    task = asyncio.create_task(go2.connect(email, password, serial, region))
    task.add_done_callback(_on_connect_done)
    return {"status": "connecting"}


@router.delete("/api/go2/connection")
async def go2_disconnect():
    global _active_rule_cb, _active_drive_cb, _autonomy_mode
    vision_loop.stop()
    drive.stop()
    _active_rule_cb  = None
    _active_drive_cb = None
    _autonomy_mode   = "manual"
    await go2.disconnect()
    _unregister_go2()
    return {"status": "disconnected"}


@router.get("/api/go2/connection")
def go2_status():
    return {
        "connected":        go2.is_connected,
        "fsm_state":        go2.fsm_state,
        "available_actions": go2.available_actions,
        "state":            go2._robot_state,
        "error":            getattr(go2, "_last_error", None),
    }


@router.get("/api/go2/video")
async def go2_video():
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 not connected")
    return StreamingResponse(
        go2.mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/api/go2/video/snapshot")
async def go2_video_snapshot():
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


@router.get("/api/go2/connection/stream")
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


class VelocityRequest(BaseModel):
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0


@router.post("/api/go2/commands")
async def go2_command(req: CommandRequest):
    try:
        await go2.send_command(req.cmd, req.params or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True}


@router.post("/api/go2/velocity")
async def go2_velocity(req: VelocityRequest):
    """WIRELESS_CONTROLLER 速度控制，无 ack，适合摇杆实时输入。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    try:
        go2.move_velocity(req.vx, req.vy, req.vyaw)
    except Exception as exc:
        logging.error("[Go2/Velocity] move_velocity 异常: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    return {"ok": True}


@router.put("/api/go2/mode")
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
    drive.user_interrupt = True
    try:
        return await run_agent(req.session_id, req.message)
    finally:
        drive.user_interrupt = False


@router.post("/api/go2/chat/stream")
async def go2_chat_stream(req: ChatRequest):
    """SSE 流：逐步推送规划、工具执行、最终回复事件。"""
    from cloud.go2.agent import run_agent_stream

    async def sse_gen():
        drive.user_interrupt = True
        try:
            async for event in run_agent_stream(req.session_id, req.message):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            drive.user_interrupt = False

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/go2/mind/last_decision")
def go2_mind_last_decision():
    """返回 ReactiveMind 上次 LLM 推理的决策结果。"""
    d = reactive_mind.last_decision
    if d is None:
        return {"decision": None}
    return {"decision": d}


@router.get("/api/go2/vision")
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


# ── 导航端点 ─────────────────────────────────────────────────────

class TagLocationRequest(BaseModel):
    name: str


class NavigateRequest(BaseModel):
    name: str


class PatrolRequest(BaseModel):
    stops: list[str]


class ObstacleAvoidanceRequest(BaseModel):
    enabled: bool


class LedRequest(BaseModel):
    color: str = "white"
    duration: int = 60


class AutonomyRequest(BaseModel):
    mode: str  # "remote" | "free_explore"


class PersonalityRequest(BaseModel):
    prompt: str


@router.post("/api/go2/navigation/locations")
async def nav_tag_location(req: TagLocationRequest):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    odom = go2.odom
    if not odom:
        raise HTTPException(status_code=503, detail="暂无 Odom 数据")
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    result = spatial_memory.tag_location(req.name, odom)
    return {"ok": True, "message": result}


@router.get("/api/go2/navigation/locations")
def nav_list_locations():
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    return spatial_memory.list_locations()


@router.delete("/api/go2/navigation/locations/{name}")
def nav_delete_location(name: str):
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    deleted = spatial_memory.delete_location(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"地点「{name}」不存在")
    return {"ok": True}


@router.post("/api/go2/navigation/go")
async def nav_go(req: NavigateRequest):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    from cloud.go2.navigation.navigator import navigator
    asyncio.create_task(navigator.go_to(req.name))
    return {"ok": True, "message": f"开始导航到「{req.name}」"}


@router.post("/api/go2/navigation/patrol")
async def nav_start_patrol(req: PatrolRequest):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if not req.stops:
        raise HTTPException(status_code=400, detail="stops 不能为空")
    from cloud.go2.navigation.navigator import navigator
    await navigator.start_patrol(req.stops)
    return {"ok": True, "stops": req.stops}


@router.delete("/api/go2/navigation/patrol")
def nav_stop():
    from cloud.go2.navigation.navigator import navigator
    navigator.stop()
    return {"ok": True}


@router.post("/api/go2/stop")
async def emergency_stop():
    global _active_rule_cb, _active_drive_cb, _autonomy_mode
    from cloud.go2.navigation.navigator import navigator
    navigator.stop()
    drive.stop()
    if _active_drive_cb is not None:
        vision_loop.remove_callback(_active_drive_cb)
        _active_drive_cb = None
    if _active_rule_cb is not None:
        vision_loop.remove_callback(_active_rule_cb)
        _active_rule_cb = None
    vision_loop.stop()
    _autonomy_mode = "manual"
    if go2.is_connected:
        go2.move_velocity(0, 0, 0)
    return {"ok": True, "mode": "manual"}


@router.get("/api/go2/navigation/state")
def nav_state():
    from cloud.go2.navigation.navigator import navigator
    s = navigator.state
    return {**s, "mode": s["mode"].value if hasattr(s["mode"], "value") else s["mode"]}


@router.put("/api/go2/obstacle-avoidance")
async def set_obstacle_avoidance(req: ObstacleAvoidanceRequest):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    await go2.set_obstacle_avoidance(req.enabled)
    return {"ok": True, "enabled": req.enabled}


@router.put("/api/go2/led")
async def set_led(req: LedRequest):
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    try:
        await go2.set_led(req.color, req.duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "color": req.color, "duration": req.duration}


@router.get("/api/go2/drive/state")
def go2_drive_state():
    return drive.state_snapshot


@router.get("/api/go2/memory")
def go2_memory():
    from cloud.go2.agentcore.memory.episode import episode_memory
    return {"entries": episode_memory.entries()}


@router.get("/api/go2/autonomy")
def go2_autonomy_get():
    return {"mode": _autonomy_mode}


@router.put("/api/go2/autonomy")
async def go2_autonomy_set(req: AutonomyRequest):
    global _active_rule_cb, _active_drive_cb, _autonomy_mode
    if req.mode not in ("manual", "reactive", "free_explore"):
        raise HTTPException(status_code=400, detail=f"未知自主模式: {req.mode}")
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    if req.mode == _autonomy_mode:
        return {"ok": True, "mode": _autonomy_mode}

    # 先清理所有现有回调和服务
    drive.stop()
    if _active_drive_cb is not None:
        vision_loop.remove_callback(_active_drive_cb)
        _active_drive_cb = None
    if _active_rule_cb is not None:
        vision_loop.remove_callback(_active_rule_cb)
        _active_rule_cb = None
    vision_loop.stop()

    if req.mode == "manual":
        logging.info("[Go2] 自主模式 → 完全遥控")
    elif req.mode == "reactive":
        _active_rule_cb = reactive_mind.on_vision_frame
        vision_loop.start(lambda: go2._latest_frame)
        vision_loop.add_callback(reactive_mind.on_vision_frame)
        logging.info("[Go2] 自主模式 → 自主反应")
    else:  # free_explore
        _active_drive_cb = drive.on_vision_frame
        vision_loop.start(lambda: go2._latest_frame)
        vision_loop.add_callback(drive.on_vision_frame)
        drive.start()
        logging.info("[Go2] 自主模式 → 自由探索")

    _autonomy_mode = req.mode
    return {"ok": True, "mode": _autonomy_mode}


@router.get("/api/go2/personality")
def go2_personality_get():
    from cloud.go2.agentcore.soul import get_system_prompt
    return {"prompt": get_system_prompt()}


@router.post("/api/go2/personality")
def go2_personality_set(req: PersonalityRequest):
    from cloud.go2.agentcore.soul import set_personality
    set_personality(req.prompt)
    return {"ok": True, "prompt": req.prompt}


@router.get("/api/go2/agent-card")
def go2_agent_card():
    """返回 Go2 智能体能力描述，供编排器动态发现技能。"""
    import inspect
    from cloud.go2.agentcore.tools.tools import TOOL_FN_MAP
    skills = []
    for name, fn in TOOL_FN_MAP.items():
        sig = inspect.signature(fn)
        skills.append({
            "name": name,
            "params": {
                k: str(v.annotation) if v.annotation is not inspect.Parameter.empty else "any"
                for k, v in sig.parameters.items()
            },
            "async": asyncio.iscoroutinefunction(fn),
        })
    return {
        "agent_id":   "go2",
        "name":       "Go2 机器狗",
        "transport":  "http",
        "base_url":   "/api/go2",
        "skills":     skills,
    }


@router.get("/api/go2/debug/voxel_map")
def go2_debug_voxel_map():
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


@router.put("/api/go2/navigation/home")
async def nav_set_home():
    """把当前位置重新标记为 home。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    odom = go2.odom
    if not odom:
        raise HTTPException(status_code=503, detail="暂无 Odom 数据")
    from cloud.go2.agentcore.memory import spatial as spatial_memory
    result = spatial_memory.tag_location("home", odom)
    return {"ok": True, "message": result}


@router.post("/api/go2/navigation/home/go")
async def nav_go_home():
    """归位：导航回 home 点。"""
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 未连接")
    from cloud.go2.navigation.navigator import navigator
    asyncio.create_task(navigator.go_to("home"))
    return {"ok": True, "message": "开始归位"}


@router.get("/api/go2/map/debug")
def go2_map_debug():
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


@router.get("/api/go2/map.png")
def go2_map_image():
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
