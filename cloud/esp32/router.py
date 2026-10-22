"""cloud.esp32.router — ESP32 桌面智能体 HTTP 端点。"""

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cloud.esp32 import agent as _esp32_agent
from cloud.api.auth import require_admin, require_admin_csrf, require_session

router = APIRouter(prefix="/api/esp32", tags=["esp32"])
_LED_UNIT_ID = os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")


def _require_led_unit(unit_id: str) -> None:
    if unit_id != _LED_UNIT_ID:
        raise HTTPException(status_code=404, detail="lamp agent is not available for this unit")


class AutonomyRequest(BaseModel):
    mode: str


class ChatRequest(BaseModel):
    text: str


@router.get("/autonomy")
async def esp32_autonomy_get(_session: dict = Depends(require_session)):
    """返回灯智能体当前自主模式（manual / reactive）。"""
    agent = _esp32_agent.get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    return {"mode": agent.get_autonomy_mode()}


@router.put("/autonomy")
async def esp32_autonomy_set(req: AutonomyRequest, _session: dict = Depends(require_admin_csrf)):
    """切换灯智能体自主模式；manual 仅停自发行动，富命令照常可控。"""
    agent = _esp32_agent.get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    try:
        agent.set_autonomy_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "mode": agent.get_autonomy_mode()}


@router.post("/{unit_id}/chat")
async def esp32_chat(unit_id: str, req: ChatRequest, _session: dict = Depends(require_admin_csrf)):
    """设备页对话直达灯智能体：控灯、调教或闲聊。"""
    _require_led_unit(unit_id)
    from cloud.api.intents import execute_lamp_chat
    return await execute_lamp_chat(unit_id, req.text.strip(), _session)


@router.get("/{unit_id}/memory")
async def esp32_memory(unit_id: str, _session: dict = Depends(require_admin)):
    """返回灯智能体的成长资产：调教记录 + 历史规律摘要。"""
    _require_led_unit(unit_id)
    from cloud.esp32.memory import taught
    agent = _esp32_agent.get_agent()
    belief = getattr(agent, "_belief_summary", "") if agent else ""
    return {"taught": taught.list_all(), "belief_summary": belief}
