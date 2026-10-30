"""Go2's public result-driven chat path; existing tool executor remains authority."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import time
import uuid
import threading
import asyncio

from langchain_core.messages import HumanMessage, SystemMessage

from cloud.agent_runtime.loop import Run, parse_decision, run_loop
from cloud.agent_runtime.replies import needs_persona_reply, persona_reply_prompt, valid_persona_reply, factual_fallback
from cloud.agent_runtime.store import RunStore
from cloud.observability import bind_context, emit_agent_event
from cloud.go2.agentcore.llm import get_text_llm
from cloud.go2.agentcore.prompts import build_prompt
from cloud.go2.agentcore.tools.executor import execute_tool
from cloud.go2.agentcore.tools.registry import TOOL_REGISTRY, tool_descriptions
from cloud.go2.connection import go2
from cloud.go2.execution import ExecutionContext
from cloud.go2.paths import DATA_DIR
from cloud.go2.space_context import prompt_space_context

_store: RunStore | None = None
logger = logging.getLogger(__name__)
_READ_ONLY = {"go2_status", "go2_observe", "go2_list_rules", "go2_list_locations",
              "go2_recognize_person", "go2_list_people", "list_peers", "get_space_context"}


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
    return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
            "result": status, "response": run.response,
            "event_id": run.reply_event_id or None,
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
                raise ValueError("unknown_public_skill")
            if skill.get("tool_name"):
                return {"tool": skill["tool_name"], "params": params or {},
                        "decision_source": "public_skill"}
        from cloud.go2.agentcore.personality.profile import get_profile
        from cloud.go2.agentcore.personality.behavior import decision_persona
        persona = decision_persona(get_profile())
        persona_source, persona_reason = "profile", None
        prompt = build_prompt(
            "loop", personality=persona, user_input=current.goal,
            tools=tool_descriptions() + '\n- ask_user: 暂停并向用户追问；参数 {"question": string}',
            device_state=(f"connected={go2.is_connected}; fsm={getattr(go2, 'fsm_state', 'unknown')}; "
                          f"space={prompt_space_context()}"),
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
        emit_agent_event("model.request", **fields, goal=current.goal,
                         model=str(getattr(model, "model_name", "unknown")),
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
        from cloud.api.intents import publish_agent_event
        from cloud.agent_runtime.progress import submit_progress
        autonomous = context.origin_kind == "agent_autonomous"
        event_session = "" if autonomous else session_id
        event_source = "autonomous" if autonomous else (
            "delegated_skill" if run.parent_run_id else "device_chat")
        progress_closed = threading.Event()
        def progress(phase, step):
            from cloud.agent_runtime.progress_facts import step_progress_facts
            result = (step or {}).get("result") or {}
            publish_agent_event("agent_progress", agent_id="go2", session_id=event_session,
                scope="space" if autonomous else "session", space_id=context.space_id,
                status=result.get("result") or phase, run_id=run.run_id,
                trace_id=run.trace_id or run.run_id, parent_run_id=run.parent_run_id,
                step_id=(step or {}).get("step_id", ""), source=event_source,
                stage=phase, tool=(step or {}).get("tool"),
                step_number=len(run.steps) if step is not None else None,
                **({"goal": run.goal,
                    "completed_count": sum((s.get("result") or {}).get("result") == "ok" for s in run.steps),
                    "failed_count": sum((s.get("result") or {}).get("result") in
                        {"blocked", "rejected", "cancelled", "error"} for s in run.steps)}
                   if step is None else {k: v for k, v in step_progress_facts(step).items() if k != "tool"}),
                event_id="evt_" + uuid.uuid5(uuid.NAMESPACE_URL,
                    f"{run.run_id}:{run.attempt}:{phase}:{(step or {}).get('step_id', '')}").hex)
            if phase == "observed" and step is not None:
                from cloud.go2.agentcore.personality.profile import get_profile
                from cloud.go2.agentcore.personality.behavior import reply_persona
                persona = reply_persona(get_profile())
                status = (step.get("result") or {}).get("result", "unknown")
                def publish_text(answer):
                    publish_agent_event("agent_progress", agent_id="go2",
                        session_id=event_session, scope="space" if autonomous else "session",
                        space_id=context.space_id, status=status, run_id=run.run_id,
                        trace_id=run.trace_id or run.run_id, parent_run_id=run.parent_run_id,
                        step_id=step["step_id"], source=event_source, stage="narrated",
                        sequence=run.attempt * 10 + 3, text=answer,
                        step_number=len(run.steps),
                        **step_progress_facts(step))
                def invoke_text(prompt):
                    from cloud.go2.agentcore.llm import get_text_llm as fresh_model
                    return asyncio.run(fresh_model().ainvoke([HumanMessage(content=prompt)])).content
                submit_progress(persona, run.goal, step, invoke_text, publish_text,
                                progress_closed, min(run.deadline, time.time() + 4))
        await run_loop(run, decide, execute, record=store.save, on_step=on_step,
                       on_progress=progress,
                       effectful=lambda name: name not in _READ_ONLY)
    offline_unconfirmed = (not go2.is_connected and run.status == "ok" and not any(
        step.get("effectful") and (step.get("result") or {}).get("result") == "ok"
        for step in run.steps))
    if offline_unconfirmed:
        # A no-step model final cannot establish that an offline robot moved.
        run.status = "blocked"
        run.response = factual_fallback("go2", "offline")
        run.reply_source = "runtime"
        store.save(run)
    if needs_persona_reply(run) and not offline_unconfirmed:
        try:
            if not go2.is_connected and any(step.get("effectful") for step in run.steps):
                raise RuntimeError("robot_disconnected")
            from cloud.go2.agentcore.personality.profile import get_profile
            from cloud.go2.agentcore.personality.behavior import reply_persona
            profile = get_profile()
            prompt = persona_reply_prompt(reply_persona(profile), run, traits=profile["traits"])
            emit_agent_event("model.request", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", session_id=session_id,
                             input=prompt, goal=run.goal, template_version="persona-result-v1")
            response = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
            emit_agent_event("model.response", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", session_id=session_id,
                             output=getattr(response, "content", ""))
            if not valid_persona_reply(response.content, run):
                raise ValueError("invalid_persona_reply")
            run.response = response.content.strip()
            run.reply_source = "persona_model"
            store.save(run)
            emit_agent_event("reply.validated", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", session_id=session_id,
                             reply=run.response, reason="persona_result_summary", reply_kind="final")
        except Exception as exc:
            logger.warning("Go2 persona reply unavailable run=%s error=%s", run.run_id, type(exc).__name__)
            run.response = factual_fallback("go2", run.status, run)
            run.reply_source = "runtime"
            store.save(run)
    # A delegated request without a reply or verified action cannot be
    # reported as complete when the physical Go2 is disconnected.
    if (run.parent_run_id and not go2.is_connected and run.status == "ok"
            and not any(step.get("effectful") and
                        (step.get("result") or {}).get("result") == "ok"
                        for step in run.steps)):
        run.status = "blocked"
        run.response = factual_fallback("go2", run.status, run)
        run.reply_source = "runtime"
        store.save(run)
    result = _result(run)
    progress_closed.set()
    progress("finished", None)
    # The offline fallback is an explicit factual reply from Go2, never a
    # model-generated claim that an unverified physical action completed.
    publish_allowed = not run.parent_run_id or time.time() < run.deadline
    if context.session_id.startswith("peer_"):
        # The messenger emits the peer reply once; keep this run's response for it.
        pass
    elif publish_allowed and result["response"]:
        published = publish_agent_event("agent_reply", agent_id="go2",
            session_id=event_session, scope="space" if autonomous else "session",
            space_id=context.space_id, status=result["result"], run_id=run.run_id,
            trace_id=run.trace_id or run.run_id, parent_run_id=run.parent_run_id,
            source=event_source,
            text=result["response"],
            reply_kind="clarification" if result["result"] == "needs_clarification" else "final")
        result["event_id"] = published.get("event_id")
        run.reply_event_id = result["event_id"] or ""
        store.save(run)
    elif publish_allowed:
        publish_agent_event("reply_error", agent_id="go2", session_id=event_session,
            scope="space" if autonomous else "session", space_id=context.space_id,
            status=result["result"], run_id=run.run_id, trace_id=run.trace_id or run.run_id,
            parent_run_id=run.parent_run_id,
            source=event_source)
    else:
        result["response"] = ""
        if not publish_allowed:
            run.response = ""
            run.reply_source = "runtime"
            store.save(run)
    if result["response"]:
        emit_agent_event("reply.emitted", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="go2", session_id=session_id,
                         reply_kind="final", channel="http", reply=result["response"])
    return result
