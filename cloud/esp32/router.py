"""cloud.esp32.router — ESP32 桌面智能体 HTTP 端点。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cloud.esp32 import agent as _esp32_agent

router = APIRouter(prefix="/api/esp32", tags=["esp32"])


class AutonomyRequest(BaseModel):
    mode: str


class ChatRequest(BaseModel):
    text: str


@router.get("/autonomy")
def esp32_autonomy_get():
    """返回灯智能体当前自主模式（manual / reactive）。"""
    agent = _esp32_agent.get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    return {"mode": agent.get_autonomy_mode()}


@router.put("/autonomy")
def esp32_autonomy_set(req: AutonomyRequest):
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
def esp32_chat(unit_id: str, req: ChatRequest):
    """设备页对话直达灯智能体：分类闲聊/调教并回复。"""
    agent = _esp32_agent.get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    return {"reply": agent.handle_user_text(req.text)}


@router.get("/{unit_id}/memory")
def esp32_memory(unit_id: str):
    """返回灯智能体的成长资产：调教记录 + 历史规律摘要。"""
    from cloud.esp32.memory import taught
    agent = _esp32_agent.get_agent()
    belief = getattr(agent, "_belief_summary", "") if agent else ""
    return {"taught": taught.list_all(), "belief_summary": belief}
