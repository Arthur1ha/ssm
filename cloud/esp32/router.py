"""cloud.esp32.router — ESP32 桌面智能体 HTTP 端点。"""

import os
import re
import time
import uuid
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cloud.esp32 import agent as _esp32_agent
from cloud.api.auth import require_admin, require_admin_csrf, require_session
from cloud.space.execution import ExecutionContext, require_service_context

router = APIRouter(prefix="/api/esp32", tags=["esp32"])
internal_router = APIRouter(prefix="/api/internal/esp32", tags=["esp32-internal"])
_LED_UNIT_ID = os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")


def _require_led_unit(unit_id: str) -> None:
    if unit_id != _LED_UNIT_ID:
        raise HTTPException(status_code=404, detail="lamp agent is not available for this unit")


class AutonomyRequest(BaseModel):
    mode: str


class ChatRequest(BaseModel):
    text: str
    run_id: str | None = None
    resume: bool = False


class SemanticTaskRequest(BaseModel):
    goal: str | None = None
    params: dict = Field(default_factory=dict)
    task_id: str
    deadline: float | None = None
    trace_id: str | None = None
    parent_run_id: str | None = None


class PhysicalReplyRequest(BaseModel):
    task_id: str
    goal: str
    action: str
    params: dict = Field(default_factory=dict)
    trace_id: str
    parent_run_id: str


class PersonalityRequest(BaseModel):
    prompt: str


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
    return await execute_lamp_chat(unit_id, req.text.strip(), _session,
                                   run_id=req.run_id, resume=req.resume)


@router.get("/{unit_id}/memory")
async def esp32_memory(unit_id: str, _session: dict = Depends(require_admin)):
    """返回灯智能体的成长资产：调教记录 + 历史规律摘要。"""
    _require_led_unit(unit_id)
    from cloud.esp32.agentcore.memory import taught
    from cloud.esp32.agentcore.memory.episode import recent
    from cloud.esp32.agentcore.memory.daily_summary import summarize_day
    agent = _esp32_agent.get_agent()
    belief = getattr(agent, "_belief_summary", "") if agent else ""
    context = ExecutionContext(_session["session_id"], "admin", True, "main",
                               "user_delegated", int(_session["expires_at"]))
    return {"taught": taught.list_all(), "belief_summary": belief,
            "episodes": recent(context), "daily_summary": summarize_day()}


@router.get("/{unit_id}/personality")
async def esp32_personality_get(unit_id: str, _session: dict = Depends(require_admin)):
    _require_led_unit(unit_id)
    from cloud.esp32.agentcore.soul import get_personality
    return {"prompt": get_personality()}


@router.post("/{unit_id}/personality")
async def esp32_personality_set(unit_id: str, req: PersonalityRequest,
                                 session: dict = Depends(require_admin_csrf)):
    _require_led_unit(unit_id)
    from cloud.esp32.agentcore.soul import set_personality, get_personality
    context = ExecutionContext(session["session_id"], "admin", True, "main",
                               "user_delegated", int(session["expires_at"]))
    try:
        set_personality(req.prompt, context)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"prompt": get_personality()}


@internal_router.post("/{unit_id}/agent/tasks", include_in_schema=False)
def invoke_lamp_semantic_task(unit_id: str, req: SemanticTaskRequest,
                              context: ExecutionContext = Depends(require_service_context)):
    """Trusted orchestration entry; semantic planning stays in the lamp brain."""
    _require_led_unit(unit_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", req.task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    if req.trace_id is not None and not re.fullmatch(r"tr_[A-Fa-f0-9]{32}", req.trace_id):
        raise HTTPException(status_code=400, detail="invalid trace_id")
    if req.parent_run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", req.parent_run_id):
        raise HTTPException(status_code=400, detail="invalid parent_run_id")
    goal = str(req.goal or req.params.get("goal") or "").strip()
    if not goal or len(goal) > 1000:
        raise HTTPException(status_code=400, detail="invalid lighting goal")
    if context.origin_kind != "user_delegated" or context.expires_at <= time.time():
        raise HTTPException(status_code=403, detail="a live delegated session is required")
    from cloud.cards.registry import get_registry
    card = get_registry().get_card(unit_id)
    if not card or not card.get("online") or not any(
        skill.get("id") == "adjust_lighting" for skill in card.get("skills", [])
    ):
        raise HTTPException(status_code=503, detail="lamp semantic skill unavailable")
    agent = _esp32_agent.get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="lamp brain unavailable")
    from cloud.esp32.tasks import run_once
    trace_id = req.trace_id or "tr_" + uuid.uuid4().hex
    def work():
        from cloud.api.intents import _dispatch_mqtt_task
        session = {"session_id": context.session_id, "actor": context.actor,
                   "expires_at": context.expires_at, "persist": False}
        def dispatch(uid, act, arguments, _context, source):
            import uuid
            from cloud.esp32.tasks import update_progress
            physical_id = "task_" + uuid.uuid4().hex
            update_progress(req.task_id, physical_id)
            _dispatch_mqtt_task(uid, act, arguments, session,
                                source=source, task_id=physical_id)
            return physical_id
        result = agent.run_user_loop(goal, unit_id, replace(context, persist=False),
                                     dispatch, lighting_only=True, run_id=req.task_id,
                                     trace_id=trace_id,
                                     parent_run_id=req.parent_run_id,
                                     deadline=req.deadline)
        return {**result, "physical_task_id": result.get("task_id")}
    # FastAPI runs synchronous route handlers in its worker pool. Waiting for
    # the result here therefore leaves the API event loop free for MQTT/SSE.
    return run_once(req.task_id, (context.session_id, unit_id, goal), work,
                    trace_id=trace_id)


@internal_router.get("/{unit_id}/agent/tasks/{task_id}", include_in_schema=False)
async def get_lamp_semantic_task(unit_id: str, task_id: str,
                                 context: ExecutionContext = Depends(require_service_context)):
    _require_led_unit(unit_id)
    from cloud.esp32.tasks import get, update_late_result
    result = get(task_id, context.session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    if result.get("result") == "timeout" and result.get("physical_task_id"):
        agent = _esp32_agent.get_agent()
        if agent:
            physical = agent._state.get_task_result(result["physical_task_id"])
            if physical:
                result = update_late_result(task_id, context.session_id, physical) or result
    return result


@internal_router.post("/{unit_id}/agent/replies", include_in_schema=False)
def reply_to_physical_task(unit_id: str, req: PhysicalReplyRequest,
                           context: ExecutionContext = Depends(require_service_context)):
    """Let the lamp describe a verified delegated MQTT result before the parent sums up."""
    _require_led_unit(unit_id)
    if context.origin_kind != "user_delegated" or context.expires_at <= time.time():
        raise HTTPException(status_code=403, detail="a live delegated session is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", req.task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    from cloud.api.main import _esp32_state
    from cloud.api.intents import publish_agent_event
    from cloud.agent_runtime.loop import Run
    from cloud.agent_runtime.replies import persona_reply_prompt, valid_persona_reply
    from cloud.esp32.agentcore.soul import load_personality
    from langchain_core.messages import HumanMessage
    agent = _esp32_agent.get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="lamp brain unavailable")
    from cloud.cards.registry import get_registry
    card = get_registry().get_card(unit_id)
    if not card or not card.get("online"):
        return {"result": "rejected", "error": "lamp_offline"}
    physical = None
    until = time.monotonic() + 2
    while physical is None and time.monotonic() < until:
        physical = _esp32_state.get_task_result(req.task_id)
        if physical is None:
            time.sleep(0.05)
    if physical is None:
        return {"result": "unknown", "error": "physical_result_unavailable"}
    status = str(physical.get("result") or "unknown")
    if status not in {"ok", "accepted", "executing", "timeout", "unknown", "blocked",
                      "rejected", "cancelled", "error"}:
        status = "unknown"
    run = Run(req.task_id, context.session_id, req.goal, time.time() + 15,
              steps=[{"step_id": req.task_id, "tool": req.action,
                      "params": req.params, "result": physical}], status=status,
              trace_id=req.trace_id, parent_run_id=req.parent_run_id,
              agent_id=unit_id, origin_kind=context.origin_kind)
    try:
        persona, _, _ = load_personality()
        answer = agent._llm.invoke([HumanMessage(content=persona_reply_prompt(persona, run))]).content
        if not valid_persona_reply(answer):
            raise ValueError("invalid_persona_reply")
    except Exception:
        publish_agent_event("reply_error", agent_id=unit_id, session_id=context.session_id,
            status=status, run_id=req.task_id, trace_id=req.trace_id,
            parent_run_id=req.parent_run_id, step_id=req.task_id, source="delegated_skill")
        return {"result": status, "response": ""}
    event = publish_agent_event("agent_reply", agent_id=unit_id,
        session_id=context.session_id, status=status, run_id=req.task_id,
        trace_id=req.trace_id, parent_run_id=req.parent_run_id,
        step_id=req.task_id, source="delegated_skill", text=answer)
    return {"result": status, "response": answer, "event_id": event["event_id"]}
