from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from cloud.esp32.agent import get_agent

router = APIRouter(prefix="/api/esp32", tags=["esp32"])


class RunRequest(BaseModel):
    session_id: str
    goal: str
    device_ids: list = []


@router.post("/intents")
async def esp32_run(req: RunRequest):
    agent = get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 Agent 未初始化")
    return await agent.run_intent(req.session_id, req.goal, req.device_ids)
