"""cloud.esp32.router — ESP32 桌面智能体 HTTP 端点。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cloud.esp32.agent import get_agent

router = APIRouter(prefix="/api/esp32", tags=["esp32"])


class AutonomyRequest(BaseModel):
    mode: str


@router.get("/autonomy")
def esp32_autonomy_get():
    """返回灯智能体当前自主模式（manual / reactive）。"""
    agent = get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    return {"mode": agent.get_autonomy_mode()}


@router.put("/autonomy")
def esp32_autonomy_set(req: AutonomyRequest):
    """切换灯智能体自主模式；manual 仅停自发行动，富命令照常可控。"""
    agent = get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    try:
        agent.set_autonomy_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "mode": agent.get_autonomy_mode()}
