"""Card-driven coordinator loop for ordinary user intents and peer requests."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path

from cloud.agents.common.runner import GraphRun as Run, run_role_graph
from cloud.agents.steward.state import build_intent_run_context
from cloud.agents.steward.prompts import build_messages, reply_prompt
from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS, make_executor
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.views import load_run, pin_completed_run
from cloud.agents.common.models import get_text_model, async_model_clients
from cloud.agents.steward.replies import finalize_steward_reply
from cloud.agents.common.messages import context_message
from cloud.agents.common.progress_facts import progress_event_facts
from cloud.observability import bind_context, emit_agent_event
from cloud.agents.steward.personality.profile import get_profile

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














def stable_reply_event_id(run: Run) -> str:
    kind = "clarification" if run.status == "needs_clarification" else "final"
    identity = f"{run.run_id}:reply:{run.model_turn}:{kind}"
    return "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex


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
                "emotion": previous.emotion,
                "steps": previous.steps}
    if resume and (not previous or previous.status != "needs_clarification"):
        return {"run_id": run_id, "result": "rejected", "error": "run_not_resumable"}
    if route and not resume:
        return {"run_id": run_id, "result": "unknown", "response": "", "steps": []}
    # Reserve the child search budget plus planning/reply time before selecting
    # tools. An explicit user/session deadline remains the hard upper bound.
    from cloud.agents.go2.workflows.search import SEARCH_SERVER_TIMEOUT_S
    budget = SEARCH_SERVER_TIMEOUT_S + 30
    deadline = min(float(context.get("expires_at") or time.time() + budget), time.time() + budget)
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
    model = None
    execute = make_executor(run, context, resume)
    def on_progress(phase: str, step: dict | None) -> None:
        transport.do_publish_ui_event(session_id, "agent_progress",
                                      **progress_event_facts(run, phase, step))

    async def finalize(current: Run) -> Run:
        from cloud.agents.steward.device_replies import publish_device_replies
        await publish_device_replies(current, context=context, transport=transport,
                                     model_factory=get_text_model)
        return await finalize_steward_reply(
            current, model=model, instructions=lambda value: reply_prompt(value, get_profile()))

    def messages_for(current: Run):
        messages = build_messages(current, supplemental)
        if context.get("selection"):
            messages.insert(1, context_message({"training_selection": context["selection"]}))
        return messages

    @async_model_clients()
    async def invoke_graph():
        nonlocal model
        model = llm or get_text_model("steward")
        return await run_role_graph(
            run, model=model, tool_schemas=STEWARD_TOOL_SCHEMAS,
            build_messages=messages_for, execute=execute,
            on_progress=on_progress, checkpointer=saver,
            finalize=finalize,
            effectful=lambda name: name in {"invoke_skill", "apply_training", "propose_training", "send_message", "start_activity",
                                              "set_space_constraint", "clear_space_constraints"},
            resume=resume, resume_value=supplemental)

    with bind_context(trace_id=run.trace_id or run.run_id, run_id=run.run_id,
                      parent_run_id=run.parent_run_id or None, agent_id="cloud_orchestrator",
                      session_id=session_id, origin_kind=run.origin_kind):
        asyncio.run(invoke_graph())
    if run.status != "needs_clarification":
        pin_completed_run(run_id, session_id, agent_id, index=index, checkpointer=saver)
    stage = ("question" if run.status == "needs_clarification" else
             "done" if run.status == "ok" else
             "partial" if run.status == "partial" else "failed")
    transport.do_publish_ui_event(session_id, "agent_reply" if run.response and run.reply_source != "runtime" else "reply_error",
        run_id=run_id, trace_id=run.trace_id or run_id, status=run.status,
        text=run.response, emotion=run.emotion, reply_source=run.reply_source,
        event_id=stable_reply_event_id(run),
        reply_kind="clarification" if run.status == "needs_clarification" else "final")
    transport.do_publish_ui_event(session_id, "run_stage", run_id=run_id,
        trace_id=run.trace_id or run_id, status=run.status, stage=stage,
        question=run.question or None, sequence=run.model_turn * 1000 + len(run.steps) * 10 + 10,
        event_id=f"{run_id}:control:{run.model_turn}:{stage}")
    on_progress("finished", None)
    if run.response:
        emit_agent_event("reply.emitted", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="cloud_orchestrator", session_id=session_id,
                         reply_kind="final", channel="mqtt_feedback", reply=run.response)
    return {"run_id": run_id, "trace_id": run.trace_id or run_id,
            "result": run.status, "response": run.response,
            "emotion": run.emotion, "reply_source": run.reply_source,
            "steps": run.steps, "question": run.question or None}
