"""Go2's public result-driven chat path; existing tool executor remains authority."""

from __future__ import annotations

import json
import hashlib
import os
import time
import uuid

from langchain_core.messages import HumanMessage, SystemMessage

from cloud.agent_runtime.loop import Run, parse_decision, run_loop
from cloud.agent_runtime.store import RunStore
from cloud.observability import bind_context, emit_agent_event
from cloud.go2.agentcore.llm import get_text_llm
from cloud.go2.agentcore.prompts import build_prompt
from cloud.go2.agentcore.soul import load_system_prompt
from cloud.go2.agentcore.tools.executor import execute_tool
from cloud.go2.agentcore.tools.registry import TOOL_REGISTRY, tool_descriptions
from cloud.go2.connection import go2
from cloud.go2.execution import ExecutionContext
from cloud.go2.paths import DATA_DIR

_store: RunStore | None = None
_READ_ONLY = {"go2_status", "go2_observe", "go2_list_rules", "go2_list_locations",
              "go2_recognize_person", "go2_list_people", "list_peers"}


def get_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore(os.getenv("SSM_GO2_RUN_DIR", str(DATA_DIR / "runs")))
    return _store


def _visible_steps(run: Run) -> str:
    return json.dumps([{k: step.get(k) for k in ("tool", "params", "result")}
                       for step in run.steps[-8:]], ensure_ascii=False, default=str)


def _result(run: Run) -> dict:
    steps = [{"tool": s["tool"], "result": s.get("result") or {"result": "unknown"},
              "step_id": s["step_id"]} for s in run.steps]
    status = run.status
    if status == "partial":
        status = next((s["result"]["result"] for s in reversed(steps)
                       if s["result"]["result"] != "ok"), "error")
    # The model's words cannot upgrade unconfirmed movement or partial work.
    response = run.response
    if status in {"accepted", "executing", "timeout", "unknown"}:
        response = "动作的实际完成情况尚未确认；后续步骤已暂停。"
    if status in {"blocked", "rejected", "error"} and steps:
        response = "部分步骤已执行；后续步骤未完成。" if any(
            s["result"]["result"] == "ok" for s in steps) else response
    return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
            "result": status, "response": response,
            "actions_taken": [s["tool"] for s in steps if s["result"]["result"] == "ok"],
            "tool_results": steps, "question": run.question or None,
            "llm_available": status != "error"}


async def run_go2_loop(session_id: str, message: str, *, context: ExecutionContext,
                       skill_id: str | None = None, params: dict | None = None,
                       run_id: str | None = None, deadline: float | None = None,
                       on_step=None, resume: bool = False,
                       trace_id: str | None = None, parent_run_id: str | None = None) -> dict:
    store = get_store()
    previous = store.get(run_id, session_id) if run_id else None
    if previous and not resume:
        return _result(previous)
    if resume:
        if previous is None or previous.status != "needs_clarification":
            return {"result": "rejected", "error": "run_not_resumable"}
        if previous.deadline <= time.time():
            return {"result": "timeout", "error": "run_expired", "run_id": previous.run_id}
        run = store.claim_resume(run_id, session_id)
        if run is None:
            return {"result": "rejected", "error": "run_already_resumed"}
        supplemental = message
        emit_agent_event("run.resumed", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="go2", session_id=session_id,
                         supplement=message)
    else:
        run = Run(run_id or uuid.uuid4().hex, session_id, message,
                  min(deadline or time.time() + 90, context.expires_at if context.expires_at else time.time() + 90))
        run.trace_id = trace_id or "tr_" + uuid.uuid4().hex
        run.parent_run_id = parent_run_id or ""
        run.agent_id = "go2"
        run.origin_kind = context.origin_kind
        supplemental = ""
        if not store.create(run):
            existing = store.get(run.run_id, session_id)
            return _result(existing) if existing else {"result": "rejected", "error": "run_id_conflict"}
        emit_agent_event("task.received", trace_id=run.trace_id,
                         run_id=run.run_id, parent_run_id=run.parent_run_id or None,
                         agent_id="go2", session_id=session_id,
                         origin_kind=run.origin_kind, input=message,
                         entry="/api/go2/chat")

    async def decide(current: Run) -> dict:
        if skill_id and not current.steps:
            from cloud.go2.card import get_card_skill
            skill = get_card_skill(skill_id)
            if not skill:
                return {"final": "该公开技能当前不可用。"}
            if skill.get("tool_name"):
                return {"tool": skill["tool_name"], "params": params or {},
                        "decision_source": "public_skill"}
        persona, persona_source, persona_reason = load_system_prompt()
        prompt = build_prompt(
            "loop", personality=persona, user_input=current.goal,
            tools=tool_descriptions() + '\n- ask_user: 暂停并向用户追问；参数 {"question": string}',
            device_state=f"connected={go2.is_connected}; fsm={getattr(go2, 'fsm_state', 'unknown')}",
            tool_results={"completed": _visible_steps(current), "supplement": supplemental},
            tool_version="go2-multitask-v1",
        )
        emit_agent_event("persona.loaded", trace_id=current.trace_id or current.run_id,
                         run_id=current.run_id, agent_id="go2", source=persona_source,
                         fallback_reason=persona_reason,
                         content_hash=hashlib.sha256(persona.encode()).hexdigest(),
                         prompt_trimmed="personality" in prompt.metadata["truncated_segments"])
        model = get_text_llm()
        turn = len(current.steps) + 1
        fields = {"trace_id": current.trace_id or current.run_id, "run_id": current.run_id,
                  "agent_id": "go2", "session_id": session_id, "turn": turn}
        emit_agent_event("model.request", **fields, model=str(getattr(model, "model_name", "unknown")),
                         template_version="go2-multitask-v1", input={"system": prompt.system, "human": prompt.human})
        started = time.monotonic()
        response = await model.ainvoke([
            SystemMessage(content=prompt.system), HumanMessage(content=prompt.human)])
        emit_agent_event("model.response", **fields,
                         output={"content": getattr(response, "content", ""),
                                 "tool_calls": getattr(response, "tool_calls", None)},
                         duration_ms=round((time.monotonic() - started) * 1000))
        try:
            return parse_decision(response)
        except (ValueError, json.JSONDecodeError):
            raise ValueError("invalid_tool_decision") from None

    async def execute(name: str, arguments: dict, step_id: str) -> dict:
        if name not in TOOL_REGISTRY:
            return {"result": "rejected", "error": "unknown_tool"}
        if name == "request_peer_skill":
            arguments = {**arguments, "request_id": arguments.get("request_id") or step_id}
        if name in {"go2_sport", "go2_move", "go2_set_led", "go2_observe"}:
            emit_agent_event("device.command", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", task_id=step_id,
                             action=name, params=arguments, transport="webrtc",
                             status="pending_execution")
        result = await execute_tool(name, arguments, context=context)
        if name in {"go2_sport", "go2_move", "go2_set_led", "go2_observe"}:
            emit_agent_event("device.result", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", task_id=step_id,
                             status=result.get("result"), result=result,
                             device_connected=bool(go2.is_connected),
                             device_fsm=str(getattr(go2, "fsm_state", "unknown")),
                             device_snapshot_at=time.time(),
                             completion_basis="webrtc_result" if result.get("result") == "ok" else "tool_boundary")
        return result

    with bind_context(trace_id=run.trace_id or run.run_id, run_id=run.run_id,
                      parent_run_id=run.parent_run_id or None, agent_id="go2",
                      session_id=session_id, origin_kind=run.origin_kind):
        await run_loop(run, decide, execute, record=store.save, on_step=on_step,
                       effectful=lambda name: name not in _READ_ONLY)
    result = _result(run)
    if result["response"] != run.response:
        emit_agent_event("reply.validated", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="go2", session_id=session_id,
                         reply= result["response"], previous_reply=run.response,
                         reason="adapter_status_guard", reply_kind="final")
    emit_agent_event("reply.emitted", trace_id=run.trace_id or run.run_id,
                     run_id=run.run_id, agent_id="go2", session_id=session_id,
                     reply_kind="final", channel="http", reply=result["response"])
    return result
