"""Card-driven coordinator loop for ordinary user intents and peer requests."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
import threading
from pathlib import Path

from langchain_core.messages import HumanMessage

from cloud.agents.common.runner import GraphRun as Run, run_role_graph
from cloud.agents.steward.state import build_intent_run_context
from cloud.agents.steward.prompts import build_messages, reply_prompt
from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS, make_executor
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.views import load_run, pin_completed_run
from cloud.agents.common.models import get_text_model
from cloud.agents.common.replies import needs_persona_reply, valid_persona_reply, factual_fallback
from cloud.agents.common.progress import submit_progress
from cloud.agents.common.progress_facts import step_progress_facts
from cloud.observability import bind_context, emit_agent_event
from cloud.agents.steward.personality.profile import get_profile
from cloud.agents.steward.personality.behavior import current_persona

from cloud.orchestrator import tools as transport

logger = logging.getLogger("agents.steward.graph")


def _checkpoint_run(saver: FileCheckpointSaver, thread_id: str, run_id: str) -> Run | None:
    """Find the latest checkpoint of this run in its session thread."""
    for item in saver.list({"configurable": {"thread_id": thread_id}}):
        values = item.checkpoint.get("channel_values", {})
        data = values.get("run")
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            continue
        run = Run(**data)
        decision = values.get("decision") or {}
        if decision.get("kind") == "ask" and run.status == "executing":
            run.status = "needs_clarification"
            run.question = decision.get("question") or ""
            run.response = run.question
        return run
    return None


def _recent_session_history(saver: FileCheckpointSaver, thread_id: str,
                            current_run_id: str, limit: int = 4) -> list[dict]:
    """Return completed user turns from this session, newest last."""
    history = []
    seen = {current_run_id}
    for item in saver.list({"configurable": {"thread_id": thread_id}}, limit=120):
        data = item.checkpoint.get("channel_values", {}).get("run")
        if not isinstance(data, dict):
            continue
        run_id = data.get("run_id")
        if not run_id or run_id in seen or data.get("status") in {"executing", "needs_clarification"}:
            continue
        seen.add(run_id)
        goal = data.get("goal")
        response = data.get("response") or data.get("decision_draft")
        if isinstance(goal, str) and goal.strip() and isinstance(response, str) and response.strip():
            history.append({"user": goal[:1000], "assistant": response[:1000]})
            if len(history) >= limit:
                break
    return list(reversed(history))














def run_intent_loop(context: dict, *, llm=None, resume: bool = False,
                    supplemental: str = "") -> dict:
    """Called in the controlled intent worker, never in an MQTT callback."""
    index = FileRunIndex()
    with index.session_lock("cloud_orchestrator", context["session_id"]):
        return _run_intent_loop(context, llm=llm, resume=resume,
                                supplemental=supplemental, index=index)


def _run_intent_loop(context: dict, *, llm, resume: bool,
                     supplemental: str, index: FileRunIndex) -> dict:
    session_id = context["session_id"]
    run_id = context.get("resume_run_id") or context.get("event_id") or uuid.uuid4().hex
    agent_id = "cloud_orchestrator"
    thread_id = thread_id_for(agent_id, session_id)
    saver = FileCheckpointSaver()
    route = index.get(run_id, session_id, agent_id=agent_id)
    previous = load_run(run_id, session_id, agent_id, index=index, checkpointer=saver) if route else None
    if previous and previous.status == "executing":
        previous = _checkpoint_run(saver, route["thread_id"], run_id)
    if previous and not resume:
        return {"run_id": run_id, "result": previous.status,
                "response": previous.response if previous.reply_source != "runtime" else "",
                "steps": previous.steps}
    if resume and (not previous or previous.status != "needs_clarification"):
        return {"run_id": run_id, "result": "rejected", "error": "run_not_resumable"}
    if route and not resume:
        return {"run_id": run_id, "result": "unknown", "response": "", "steps": []}
    # A visual target task includes detection, closed-loop travel and a fresh
    # arrival check. Keep the user session as the hard upper bound.
    deadline = min(float(context.get("expires_at") or time.time() + 180), time.time() + 180)
    if resume:
        run = previous
        run.attempt += 1
        emit_agent_event("run.resumed", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="cloud_orchestrator",
                         session_id=session_id, supplement=supplemental)
    else:
        latest = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if latest:
            values = latest.checkpoint.get("channel_values", {})
            other_run = values.get("run") or {}
            awaiting_answer = (values.get("decision") or {}).get("kind") == "ask"
            still_running = (other_run.get("status") == "executing" and
                             (awaiting_answer or float(other_run.get("deadline") or 0) > time.time()))
            if other_run.get("run_id") != run_id and still_running:
                return {"run_id": run_id, "result": "rejected", "error": "session_busy"}
        run = Run(run_id, session_id, context["user_msg"], deadline)
        run.trace_id = context.get("trace_id") or "tr_" + uuid.uuid4().hex
        run.parent_run_id = context.get("parent_run_id") or ""
        run.agent_id = "cloud_orchestrator"
        run.origin_kind = context.get("origin_kind") or "user_delegated"
        try:
            index.put_if_absent(run_id, agent_id, session_id, thread_id)
        except ValueError:
            return {"run_id": run_id, "result": "rejected", "error": "run_id_conflict"}
    context = {**context, "source_text": supplemental if resume else context["user_msg"]}
    if resume:
        context = {**context, "user_msg": run.goal}
    history = _recent_session_history(saver, thread_id, run_id)
    model = llm or get_text_model("steward")
    execute = make_executor(run, context, resume)
    transport.do_publish_feedback(session_id, "planning", "正在理解并执行目标…", run_id=run_id)



    def on_step(step: dict) -> None:
        result = step["result"]
        transport.do_publish_feedback(session_id, "step", "步骤执行结果已更新。",
                                      run_id=run_id, step_id=step["step_id"],
                                      step_number=len(run.steps),
                                      tool=step["tool"], result=result,
                                      status=result["result"])

    progress_closed = threading.Event()

    def on_progress(phase: str, step: dict | None) -> None:
        if step is None:
            transport.do_publish_ui_event(session_id, "agent_progress", run_id=run_id,
                trace_id=run.trace_id or run_id, status=phase, stage=phase,
                sequence=run.attempt, goal=run.goal,
                completed_count=sum((s.get("result") or {}).get("result") == "ok" for s in run.steps),
                failed_count=sum((s.get("result") or {}).get("result") in
                    {"blocked", "rejected", "cancelled", "error"} for s in run.steps))
        else:
            result = step.get("result") or {}
            transport.do_publish_ui_event(session_id, "agent_progress", run_id=run_id,
                trace_id=run.trace_id or run_id, status=result.get("result") or phase,
                step_id=step["step_id"], stage=phase,
                sequence=run.attempt * 10 + {"decision": 0, "dispatching": 1,
                    "observed": 2, "considering": 4}.get(phase, 3),
                step_number=len(run.steps),
                **step_progress_facts(step))
            if phase == "observed":
                status = result.get("result", "unknown")
                def invoke_text(prompt):
                    return get_text_model("steward").invoke([HumanMessage(content=prompt)]).content
                def publish_text(answer):
                    transport.do_publish_ui_event(session_id, "agent_progress",
                        run_id=run_id, trace_id=run.trace_id or run_id,
                        status=status, text=answer, step_id=step["step_id"],
                        stage="narrated", sequence=run.attempt * 10 + 3,
                        step_number=len(run.steps),
                        **step_progress_facts(step))
                submit_progress(current_persona(), run.goal, step, invoke_text,
                                publish_text, progress_closed,
                                min(run.deadline, time.time() + 4))

    async def finalize(current: Run) -> Run:
        if not needs_persona_reply(current):
            return current
        try:
            profile = get_profile()
            prompt = reply_prompt(current, profile)
            emit_agent_event("model.request", trace_id=current.trace_id or current.run_id,
                             run_id=current.run_id, agent_id="cloud_orchestrator", session_id=session_id,
                             input=prompt, goal=current.goal,
                             model=str(getattr(model, "model_name", "unknown")),
                             template_version="persona-result-v1")
            response = await model.ainvoke([HumanMessage(content=prompt)])
            emit_agent_event("model.response", trace_id=current.trace_id or current.run_id,
                             run_id=current.run_id, agent_id="cloud_orchestrator", session_id=session_id,
                             output=getattr(response, "content", ""))
            if not valid_persona_reply(response.content, current):
                raise ValueError("invalid_persona_reply")
            current.response = response.content.strip()
            current.reply_source = "persona_model"
            emit_agent_event("reply.validated", trace_id=current.trace_id or current.run_id,
                             run_id=current.run_id, agent_id="cloud_orchestrator", session_id=session_id,
                             reply=current.response, reason="persona_result_summary", reply_kind="final")
        except Exception as exc:
            logger.warning("orchestrator persona reply unavailable run=%s error=%s",
                           current.run_id, type(exc).__name__)
            current.response = factual_fallback("cloud_orchestrator", current.status, current)
            current.reply_source = "runtime"
        return current

    with bind_context(trace_id=run.trace_id or run.run_id, run_id=run.run_id,
                      parent_run_id=run.parent_run_id or None, agent_id="cloud_orchestrator",
                      session_id=session_id, origin_kind=run.origin_kind):
        asyncio.run(run_role_graph(
            run, model=model, tool_schemas=STEWARD_TOOL_SCHEMAS,
            build_messages=lambda current: build_messages(current, supplemental, history) + ([HumanMessage(content="选中的可信转换：" + str(context["selection"]))] if context.get("selection") else []), execute=execute,
            on_step=on_step, on_progress=on_progress, checkpointer=saver,
            finalize=finalize,
            effectful=lambda name: name in {"invoke_skill", "propose_training", "send_message", "start_activity",
                                              "set_space_constraint", "clear_space_constraints"},
            resume=resume, resume_value=supplemental))
    if run.status != "needs_clarification":
        pin_completed_run(run_id, session_id, agent_id, index=index, checkpointer=saver)
    stage = ("question" if run.status == "needs_clarification" else
             "done" if run.status == "ok" else
             "partial" if run.status == "partial" else "failed")
    progress_closed.set()
    transport.do_publish_feedback(session_id, stage, run.response, run_id=run_id,
                                  trace_id=run.trace_id or run_id,
                                  status=run.status, question=run.question or None)
    transport.do_publish_ui_event(session_id, "agent_progress", run_id=run_id,
        trace_id=run.trace_id or run_id, status=run.status, stage="finished",
        sequence=run.attempt, goal=run.goal,
        completed_count=sum((s.get("result") or {}).get("result") == "ok" for s in run.steps),
        failed_count=sum((s.get("result") or {}).get("result") in
            {"blocked", "rejected", "cancelled", "error"} for s in run.steps))
    transport.do_publish_ui_event(session_id, "agent_reply" if run.response else "reply_error",
        run_id=run_id, trace_id=run.trace_id or run_id, status=run.status,
        text=run.response,
        reply_kind="clarification" if run.status == "needs_clarification" else "final")
    if run.response:
        emit_agent_event("reply.emitted", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="cloud_orchestrator", session_id=session_id,
                         reply_kind="final", channel="mqtt_feedback", reply=run.response)
    return {"run_id": run_id, "trace_id": run.trace_id or run_id,
            "result": run.status, "response": run.response,
            "steps": run.steps, "question": run.question or None}
