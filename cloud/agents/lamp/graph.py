"""LangGraph conversation entry for the desk lamp."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid

from langchain_core.messages import HumanMessage

from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.views import load_run, pin_completed_run
from cloud.agents.lamp.prompts import (decision_messages, persona_reply_prompt,
                                       valid_lamp_reply, has_unrequested_light_parameters,
                                       has_internal_terms)
from cloud.agents.lamp.state import READ_ONLY_TOOLS
from cloud.agents.lamp.tools.execute import execute_lamp_tool
from cloud.agents.lamp.tools.schemas import visible_schemas
from cloud.agents.common.progress import submit_progress
from cloud.agents.common.progress_facts import step_progress_facts
from cloud.agents.common.replies import factual_fallback, needs_persona_reply
from cloud.agents.lamp.personality.profile import get_profile
from cloud.agents.lamp.personality.soul import load_personality
from cloud.esp32.physical_tasks import get as get_physical_task, stable_task_id
from cloud.cards import registry as cards
from cloud.observability import bind_context, emit_agent_event
from cloud.space.execution import ExecutionContext

logger = logging.getLogger(__name__)


def _response(run, *, runtime_reply: bool = False) -> dict:
    status = _public_status(run)
    return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
            "result": status,
            "reply": "" if runtime_reply and run.reply_source == "runtime" else run.response,
            "event_id": run.reply_event_id or stable_reply_event_id(run, status),
            "question": run.question or None, "steps": run.steps,
            "task_id": next((s.get("result", {}).get("task_id") for s in reversed(run.steps)
                             if s.get("result", {}).get("task_id")), None)}


def _public_status(run: GraphRun) -> str:
    if run.status != "partial":
        return run.status
    return next((step["result"]["result"] for step in reversed(run.steps)
                 if step.get("result", {}).get("result") != "ok"), "error")


def stable_reply_event_id(run: GraphRun, status: str) -> str | None:
    if not run.response or run.session_id.startswith("peer_"):
        return None
    scope = "space" if run.origin_kind == "agent_autonomous" else "session"
    session_id = "" if scope == "space" else run.session_id
    reply_kind = "clarification" if status == "needs_clarification" else "final"
    stable_key = ":".join(("agent_reply", scope, session_id or "main", run.run_id,
                           run.agent_id, "", reply_kind, "", ""))
    return "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, stable_key).hex


def run_lamp_loop(agent, text: str, unit_id: str, context: ExecutionContext,
                  dispatch, *, lighting_only: bool = False,
                  run_id: str | None = None, resume: bool = False,
                  trace_id: str | None = None,
                  parent_run_id: str | None = None,
                  deadline: float | None = None, selection: dict | None = None) -> dict:
    """Run one lamp Graph thread against observed ESP32 and peer results."""
    if (context.space_id != "main"
            or context.origin_kind not in {"user_delegated", "agent_autonomous"}
            or context.actor not in {"admin", "visitor", "agent"}
            or (context.origin_kind == "agent_autonomous" and context.persist)
            or context.expires_at <= time.time()):
        return {"result": "rejected", "error": "invalid_context", "reply": ""}
    index = FileRunIndex()
    with index.session_lock(unit_id, context.session_id):
        return _run_lamp_loop_locked(
            agent, text, unit_id, context, dispatch, index=index,
            lighting_only=lighting_only, run_id=run_id, resume=resume,
            trace_id=trace_id, parent_run_id=parent_run_id, deadline=deadline, selection=selection)


def _run_lamp_loop_locked(agent, text: str, unit_id: str, context: ExecutionContext,
                          dispatch, *, index: FileRunIndex, lighting_only: bool,
                          run_id: str | None, resume: bool,
                          trace_id: str | None, parent_run_id: str | None,
                          deadline: float | None, selection: dict | None) -> dict:
    saver = FileCheckpointSaver()
    thread_id = thread_id_for(unit_id, context.session_id)
    previous = (load_run(run_id, context.session_id, unit_id,
                         index=index, checkpointer=saver) if run_id else None)
    if previous and not resume:
        return _response(previous, runtime_reply=True)
    head = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    head_values = head.checkpoint.get("channel_values", {}) if head else {}
    head_run = head_values.get("run") or {}
    if resume and (previous is None or previous.status != "needs_clarification"
                   or head_run.get("run_id") != run_id):
        return {"result": "rejected", "error": "run_not_resumable"}
    if not resume and head_run.get("run_id") != run_id and (
            head_run.get("status") == "executing" or
            (head_values.get("decision") or {}).get("kind") == "ask"):
        return {"result": "rejected", "error": "session_busy"}
    if run_id and not resume and index.get(run_id, context.session_id, agent_id=unit_id):
        return {"result": "unknown", "error": "run_state_unavailable"}
    if resume:
        run = previous
        run.attempt += 1
        run.reply_ready = False
        emit_agent_event("run.resumed", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id=unit_id,
                         session_id=context.session_id, supplement=text)
    else:
        run = GraphRun(run_id or uuid.uuid4().hex, context.session_id,
                       text, min(float(context.expires_at), deadline or time.time() + 90))
        run.reply_ready = False
        run.trace_id = trace_id or "tr_" + uuid.uuid4().hex
        run.parent_run_id = parent_run_id or ""
        run.agent_id = unit_id
        run.origin_kind = context.origin_kind
        try:
            index.put_if_absent(run.run_id, unit_id, context.session_id, thread_id)
        except ValueError:
            return {"result": "rejected", "error": "run_id_conflict"}
        emit_agent_event("task.received", trace_id=run.trace_id,
                         run_id=run.run_id, parent_run_id=run.parent_run_id or None,
                         agent_id=unit_id, session_id=context.session_id,
                         origin_kind=run.origin_kind, input=text,
                         entry="lamp_semantic" if lighting_only else "lamp_chat")
    if context.origin_kind == "user_delegated":
        agent.mark_user_command(unit_id)

    from cloud.api.intents import publish_agent_event
    autonomous = context.origin_kind == "agent_autonomous"
    event_session = "" if autonomous else context.session_id
    event_source = ("autonomous" if autonomous else
                    "delegated_skill" if run.parent_run_id else "device_chat")
    progress_closed = threading.Event()
    run_model = agent._llm_for_run() if hasattr(agent, "_llm_for_run") else agent._llm

    def progress(phase, step):
        result = (step or {}).get("result") or {}
        facts = ({"goal": run.goal,
                  "completed_count": sum((s.get("result") or {}).get("result") == "ok" for s in run.steps),
                  "failed_count": sum((s.get("result") or {}).get("result") in
                                      {"blocked", "rejected", "cancelled", "error"} for s in run.steps)}
                 if step is None else {k: v for k, v in step_progress_facts(step).items() if k != "tool"})
        publish_agent_event(
            "agent_progress", agent_id=unit_id, session_id=event_session,
            scope="space" if autonomous else "session", space_id=context.space_id,
            status=result.get("result") or phase, run_id=run.run_id,
            trace_id=run.trace_id or run.run_id, parent_run_id=run.parent_run_id,
            step_id=(step or {}).get("step_id", ""), source=event_source,
            stage=phase, tool=(step or {}).get("tool"),
            step_number=len(run.steps) if step is not None else None,
            event_id="evt_" + uuid.uuid5(uuid.NAMESPACE_URL,
                f"{run.run_id}:{run.attempt}:{phase}:{(step or {}).get('step_id', '')}").hex,
            **facts,
        )
        if phase == "observed" and step is not None:
            persona, _, _ = load_personality()
            status = result.get("result", "unknown")

            def publish_text(answer):
                if (has_unrequested_light_parameters(answer, run.goal)
                        or has_internal_terms(answer)):
                    return
                publish_agent_event("agent_progress", agent_id=unit_id,
                    session_id=event_session, scope="space" if autonomous else "session",
                    space_id=context.space_id, status=status, run_id=run.run_id,
                    trace_id=run.trace_id or run.run_id, parent_run_id=run.parent_run_id,
                    step_id=step["step_id"], source=event_source, stage="narrated",
                    sequence=run.attempt * 10 + 3, text=answer,
                    step_number=len(run.steps), **step_progress_facts(step))

            def invoke_text(prompt):
                return get_text_llm(ChatOpenAI).invoke([HumanMessage(content=prompt)]).content

            submit_progress(persona + "\n进展只用自然语言，不展示 RGB、亮度或色温数值。",
                            run.goal, step, invoke_text, publish_text,
                            progress_closed, min(run.deadline, time.time() + 4))

    async def execute(name, params, step_id):
        if name in {"list_training", "propose_training"}:
            from dataclasses import asdict
            from cloud.agents.common.training.tools import execute_training
            if run.parent_run_id or context.session_id.startswith("peer_"):
                return {"result": "rejected", "error": "direct_user_request_required"}
            return await execute_training(name, params, {**asdict(context), "run_id": run.run_id,
                                                        "source_text": text, "selection": selection})
        return execute_lamp_tool(name, params, step_id, agent=agent, unit_id=unit_id,
                                 context=context, dispatch=dispatch,
                                 lighting_only=lighting_only, run_id=run.run_id)

    def reconcile(name, params, step_id):
        if name in READ_ONLY_TOOLS:
            return None
        return get_physical_task(stable_task_id(unit_id, run.run_id, step_id),
                                 unit_id=unit_id, session_id=context.session_id)

    async def finalize(current: GraphRun) -> GraphRun:
        if current.status == "error" and not current.steps:
            # The decision model failed before dispatch. There is no device
            # receipt for a second model to interpret or invent.
            current.response = factual_fallback(unit_id, current.status, current)
            current.reply_source = "runtime"
        elif needs_persona_reply(current) and (not current.parent_run_id or time.time() < current.deadline):
            try:
                prompt = persona_reply_prompt(current, get_profile())
                emit_agent_event("model.request", trace_id=current.trace_id or current.run_id,
                                 run_id=current.run_id, agent_id=unit_id, session_id=current.session_id,
                                 input=prompt, goal=current.goal,
                                 model=str(getattr(run_model, "model_name", "unknown")),
                                 template_version="persona-result-v1")
                response = await run_model.ainvoke([HumanMessage(content=prompt)])
                emit_agent_event("model.response", trace_id=current.trace_id or current.run_id,
                                 run_id=current.run_id, agent_id=unit_id, session_id=current.session_id,
                                 output=getattr(response, "content", ""))
                if not valid_lamp_reply(response.content, current):
                    raise ValueError("invalid_persona_reply")
                current.response = response.content.strip()
                current.reply_source = "persona_model"
                emit_agent_event("reply.validated", trace_id=current.trace_id or current.run_id,
                                 run_id=current.run_id, agent_id=unit_id,
                                 session_id=current.session_id, reply=current.response,
                                 reason="persona_result_summary", reply_kind="final")
            except Exception as exc:
                logger.warning("lamp persona reply unavailable run=%s error=%s",
                               current.run_id, type(exc).__name__)
                current.response = factual_fallback(unit_id, current.status, current)
                current.reply_source = "runtime"
        if current.parent_run_id and time.time() >= current.deadline:
            current.response = ""
            current.reply_source = "runtime"
        current.reply_ready = True
        if not context.session_id.startswith("peer_"):
            if current.response:
                current.reply_event_id = stable_reply_event_id(
                    current, _public_status(current)) or ""
                publish_agent_event(
                    "agent_reply", agent_id=unit_id, session_id=event_session,
                    scope="space" if autonomous else "session", space_id=context.space_id,
                    status=_public_status(current), run_id=current.run_id,
                    trace_id=current.trace_id or current.run_id,
                    parent_run_id=current.parent_run_id, source=event_source,
                    text=current.response, reply_kind="final",
                    event_id=current.reply_event_id)
            else:
                publish_agent_event(
                    "reply_error", agent_id=unit_id, session_id=event_session,
                    scope="space" if autonomous else "session", space_id=context.space_id,
                    status=current.status, run_id=current.run_id,
                    trace_id=current.trace_id or current.run_id,
                    parent_run_id=current.parent_run_id, source=event_source)
        return current

    with bind_context(trace_id=run.trace_id or run.run_id, run_id=run.run_id,
                      parent_run_id=run.parent_run_id or None, agent_id=unit_id,
                      session_id=context.session_id, origin_kind=run.origin_kind):
        run = asyncio.run(run_role_graph(
            run, model=run_model,
            tool_schemas=visible_schemas(lighting_only=lighting_only,
                                        card=cards.get_registry().get_card(unit_id)),
            build_messages=lambda current: decision_messages(
                current, agent=agent, unit_id=unit_id, supplement=(text if resume else "") + ("\n选中转换：" + str(selection) if selection else "")),
            execute=execute, reconcile_effect=reconcile,
            finalize=finalize, checkpointer=saver, on_progress=progress,
            effectful=lambda name: name not in READ_ONLY_TOOLS and name != "list_training",
            resume=resume, resume_value=text if resume else None,
        ))
    if run.status != "needs_clarification":
        pin_completed_run(run.run_id, context.session_id, unit_id,
                          index=index, checkpointer=saver)
    progress_closed.set()
    progress("finished", None)
    status = _public_status(run)
    reply = run.response
    if context.session_id.startswith("peer_"):
        published = {}
    elif run.status == "needs_clarification" and reply:
        run.reply_event_id = stable_reply_event_id(run, status) or ""
        publish_agent_event(
            "agent_reply", agent_id=unit_id, session_id=event_session,
            scope="space" if autonomous else "session", space_id=context.space_id,
            status=status, run_id=run.run_id, trace_id=run.trace_id or run.run_id,
            parent_run_id=run.parent_run_id, source=event_source, text=reply,
            reply_kind="clarification", event_id=run.reply_event_id,
        )
    if reply:
        emit_agent_event("reply.emitted", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id=unit_id, session_id=run.session_id,
                         reply_kind="final", channel="http", reply=reply)
    return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
            "result": status, "reply": reply,
            "event_id": run.reply_event_id or None,
            "question": run.question or None, "steps": run.steps,
            "task_id": next((s.get("result", {}).get("task_id") for s in reversed(run.steps)
                             if s.get("result", {}).get("task_id")), None)}
