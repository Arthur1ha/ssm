"""Go2's LangGraph conversation entry; device executor remains authority."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
import threading
import asyncio

from langchain_core.messages import HumanMessage

from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.go2.run_view import Go2RunView, stable_reply_event_id
from cloud.agents.go2.prompts import build_messages as go2_messages, persona_reply_prompt
from cloud.agents.go2.tools.declarations import READ_ONLY_TOOLS, tool_schemas
from cloud.agents.common.replies import needs_persona_reply, valid_persona_reply, factual_fallback
from cloud.observability import bind_context, emit_agent_event
from cloud.go2.models import get_text_llm
from cloud.go2.tools.executor import execute_tool
from cloud.go2.tools.registry import TOOL_REGISTRY
from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.go2.execution import ExecutionContext
from cloud.agents.go2.memory.episode import episode_memory
from cloud.agents.go2.memory.recall import recall_memory

_view: Go2RunView | None = None
logger = logging.getLogger(__name__)


def get_store() -> Go2RunView:
    """Compatibility name for task-status callers; state lives in checkpoints."""
    global _view
    if _view is None:
        _view = Go2RunView()
    return _view


def _result(run: GraphRun) -> dict:
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
                       trace_id: str | None = None, parent_run_id: str | None = None,
                       allowed_tools: frozenset[str] | None = None,
                       tool_guard=None, task_context=None,
                       publish_reply: bool = True, selection: dict | None = None) -> dict:
    view = get_store()
    async with view.index.async_session_lock("go2", session_id):
        return await _run_go2_loop_locked(
            session_id, message, context=context, view=view,
            skill_id=skill_id, params=params, run_id=run_id, deadline=deadline,
            on_step=on_step, resume=resume, trace_id=trace_id,
            parent_run_id=parent_run_id, allowed_tools=allowed_tools,
            tool_guard=tool_guard, task_context=task_context,
            publish_reply=publish_reply, selection=selection)


async def _run_go2_loop_locked(session_id: str, message: str, *, context: ExecutionContext,
                               view: Go2RunView, skill_id: str | None,
                               params: dict | None, run_id: str | None,
                               deadline: float | None, on_step, resume: bool,
                               trace_id: str | None, parent_run_id: str | None,
                               allowed_tools: frozenset[str] | None,
                               tool_guard, task_context, publish_reply: bool, selection: dict | None) -> dict:
    # Resolve public requests before registering a run or claiming device work.
    # CHAT is an explicit conversation entry, not a missing hardware tool.
    initial_tool = None
    if skill_id:
        from cloud.go2.tools.registry import get_card_skill
        from cloud.go2.tools.executor import validate_params
        skill = get_card_skill(skill_id)
        if skill is None:
            return {"result": "rejected", "error": "unknown_public_skill"}
        try:
            public_params = validate_params(skill["params_schema"], params if params is not None else {})
        except ValueError as exc:
            return {"result": "rejected", "error": "invalid_skill_parameters", "message": str(exc)}
        tool_name = skill.get("tool_name")
        if tool_name is None and (skill.get("invoke") or {}).get("action") == "CHAT":
            message = public_params["message"]
        elif tool_name in TOOL_REGISTRY:
            initial_tool = (tool_name, public_params)
            if allowed_tools is not None and tool_name not in allowed_tools:
                return {"result": "rejected", "error": "tool_not_allowed_for_task"}
        else:
            return {"result": "rejected", "error": "unknown_public_skill"}

    await view.reconcile_interrupted(session_id)
    previous = view.get(run_id, session_id, require_head=resume) if run_id else None
    if previous and not resume:
        return _result(previous)
    if resume:
        if previous is None or previous.status != "needs_clarification":
            return {"result": "rejected", "error": "run_not_resumable"}
        if previous.deadline <= time.time():
            return {"result": "timeout", "error": "run_expired", "run_id": previous.run_id}
        run = previous
        supplemental = message
        emit_agent_event("run.resumed", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="go2", session_id=session_id,
                         supplement=message)
    else:
        run = GraphRun(run_id or uuid.uuid4().hex, session_id, message,
                  min(deadline or time.time() + 180, context.expires_at if context.expires_at else time.time() + 180))
        if view.session_busy(run.run_id, session_id):
            return {"result": "rejected", "error": "session_busy", "run_id": run.run_id}
        run.trace_id = trace_id or "tr_" + uuid.uuid4().hex
        run.parent_run_id = parent_run_id or ""
        run.agent_id = "go2"
        run.origin_kind = context.origin_kind
        supplemental = ""
        try:
            created = view.register(run.run_id, session_id)
        except ValueError:
            return {"result": "rejected", "error": "run_id_conflict"}
        if not created:
            existing = view.get(run.run_id, session_id)
            return _result(existing) if existing else {"result": "unknown", "error": "run_state_unavailable"}
        emit_agent_event("task.received", trace_id=run.trace_id,
                         run_id=run.run_id, parent_run_id=run.parent_run_id or None,
                         agent_id="go2", session_id=session_id,
                         origin_kind=run.origin_kind, input=message,
                         entry="/api/go2/chat")


    def build_messages(current: GraphRun):
        from cloud.agents.go2.personality.profile import get_profile
        profile = get_profile()
        remembered = recall_memory(
            current.goal + (" " + supplemental if supplemental else ""),
            actor=context.actor, persist=context.persist, session_id=session_id,
            episode_store=episode_memory)
        messages = go2_messages(current, supplement=supplemental,
                                memory=remembered.text if remembered.fragments else "")
        if selection:
            messages.append(HumanMessage(content="选中的可信转换：" + str(selection)))
        if task_context is not None:
            messages.append(HumanMessage(content="【本轮可信任务状态】\n" + str(task_context())))
        emit_agent_event("persona.loaded", trace_id=current.trace_id or current.run_id,
                         run_id=current.run_id, agent_id="go2", source="profile",
                         revision=profile.get("revision"),
                         content_hash=hashlib.sha256(profile["prompt"].encode()).hexdigest(),
                         prompt_trimmed=False)
        return messages

    interaction_token = None
    motion_unconfirmed = False
    runtime = runtime_for(go2)
    # Explicit read/life skills keep status lookup and wake reachable without
    # claiming an interaction. Other accepted Graph tasks own play until their
    # true terminal or clarification event, including time spent waiting on LLM.
    read_or_life = initial_tool is not None and (initial_tool[0] in READ_ONLY_TOOLS or initial_tool[0] == 'go2_life' or TOOL_REGISTRY[initial_tool[0]].motion)
    if runtime is not None and not runtime.active_reason() and not read_or_life:
        if context.origin_kind != 'agent_autonomous':
            from cloud.go2.service import prepare_user_interaction
            try:
                await prepare_user_interaction(context)
            except ValueError as exc:
                return {'result': 'blocked', 'error': str(exc), 'run_id': run.run_id}
        if runtime.data['life_state'] == 'idle':
            interaction_token = runtime.begin_life('play', context.session_id, run.run_id)

    async def execute(name: str, arguments: dict, step_id: str) -> dict:
        nonlocal interaction_token, motion_unconfirmed
        if name in {"list_training", "propose_training"}:
            from dataclasses import asdict
            from cloud.agents.common.training.tools import execute_training
            if allowed_tools is not None or skill_id or run.parent_run_id or session_id.startswith("peer_"):
                return {"result": "rejected", "error": "direct_user_request_required"}
            return await execute_training(name, arguments, {**asdict(context), "run_id": run.run_id,
                                                            "source_text": message, "selection": selection})
        if name not in TOOL_REGISTRY:
            return {"result": "rejected", "error": "unknown_tool"}
        if allowed_tools is not None and name not in allowed_tools:
            return {"result": "rejected", "error": "tool_not_allowed_for_task"}
        if tool_guard is not None:
            blocked = tool_guard(name, arguments)
            if blocked is not None:
                return blocked
        if name == "request_peer_skill":
            arguments = {**arguments, "request_id": arguments.get("request_id") or step_id}
        if name in {"go2_sport", "go2_move", "go2_set_led", "go2_observe"}:
            emit_agent_event("device.command", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", task_id=step_id,
                             action=name, params=arguments, transport="webrtc",
                             status="pending_execution")
        runtime = runtime_for(go2)
        if (name not in READ_ONLY_TOOLS and name != 'go2_life' and runtime is not None
                and not TOOL_REGISTRY[name].motion):
            if not runtime.active_reason() and runtime.data['life_state'] == 'idle':
                interaction_token = runtime.begin_life('play', context.session_id, run.run_id)
        # A life command is itself the requested task, so release this Graph's
        # completed interaction before evaluating idle-only rest.
        if name == 'go2_life' and interaction_token is not None:
            runtime.finish_life(interaction_token)
            interaction_token = None
        from cloud.agents.go2.workflows.search import search_request
        with search_request(run.run_id, run.deadline):
            result = await execute_tool(name, arguments, context=context)
        if TOOL_REGISTRY[name].motion:
            motion_unconfirmed = (result.get("result") in {"accepted", "executing", "unknown", "timeout"}
                                  or result.get("reason") == "stop_unconfirmed")
        if name in {"go2_sport", "go2_move", "go2_set_led", "go2_observe"}:
            result = {**result, "device_connected": bool(go2.is_connected),
                      "device_fsm": getattr(go2, "fsm_state", None)}
            emit_agent_event("device.result", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", task_id=step_id,
                             status=result.get("result"), result=result,
                             device_connected=bool(go2.is_connected),
                             device_fsm=getattr(go2, "fsm_state", None),
                             device_snapshot_at=time.time(),
                             completion_basis=(result.get("confirmation") or "webrtc_result")
                             if result.get("result") == "ok" else "tool_boundary")
        return result

    with bind_context(trace_id=run.trace_id or run.run_id, run_id=run.run_id,
                      parent_run_id=run.parent_run_id or None, agent_id="go2",
                      session_id=session_id, origin_kind=run.origin_kind):
        from cloud.api.intents import publish_agent_event
        from cloud.agents.common.progress import submit_progress
        autonomous = context.origin_kind == "agent_autonomous"
        event_session = "" if autonomous else session_id
        event_source = "autonomous" if autonomous else (
            "delegated_skill" if run.parent_run_id else "device_chat")
        progress_closed = threading.Event()
        def progress(phase, step):
            from cloud.agents.common.progress_facts import step_progress_facts
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
            if publish_reply and phase == "observed" and step is not None:
                from cloud.agents.go2.personality.profile import get_profile
                from cloud.agents.go2.personality.behavior import reply_persona
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
                    from cloud.go2.models import get_text_llm as fresh_model
                    return asyncio.run(fresh_model().ainvoke([HumanMessage(content=prompt)])).content
                if (step.get("tool") == "go2_navigate_to_visible_target"
                        and status in {"ok", "blocked", "cancelled"}
                        and isinstance(result.get("message"), str)):
                    return
                submit_progress(persona, run.goal, step, invoke_text, publish_text,
                                progress_closed, min(run.deadline, time.time() + 4))
        async def finalize(current: GraphRun) -> GraphRun:
            """Validate facts, speak as Go2 and checkpoint the user-visible reply."""
            confirmed = any(step.get("effectful") and
                            (step.get("result") or {}).get("result") == "ok"
                            for step in current.steps)
            offline_unconfirmed = not go2.is_connected and current.status == "ok" and not confirmed
            if offline_unconfirmed:
                current.status = "blocked"
            if needs_persona_reply(current):
                try:
                    from cloud.agents.go2.personality.profile import get_profile
                    profile = get_profile()
                    prompt = persona_reply_prompt(current, profile)
                    emit_agent_event("model.request", trace_id=current.trace_id or current.run_id,
                                     run_id=current.run_id, agent_id="go2", session_id=session_id,
                                     input=prompt, goal=current.goal, template_version="persona-result-v1")
                    response = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
                    emit_agent_event("model.response", trace_id=current.trace_id or current.run_id,
                                     run_id=current.run_id, agent_id="go2", session_id=session_id,
                                     output=getattr(response, "content", ""))
                    if not valid_persona_reply(response.content, current):
                        raise ValueError("invalid_persona_reply")
                    current.response = response.content.strip()
                    current.reply_source = "persona_model"
                    emit_agent_event("reply.validated", trace_id=current.trace_id or current.run_id,
                                     run_id=current.run_id, agent_id="go2", session_id=session_id,
                                     reply=current.response, reason="persona_result_summary", reply_kind="final")
                except Exception as exc:
                    logger.warning("Go2 persona reply unavailable run=%s error=%s",
                                   current.run_id, type(exc).__name__)
                    last_step = current.steps[-1] if current.steps else {}
                    last_result = last_step.get("result") or {}
                    if (last_step.get("tool") == "go2_navigate_to_visible_target"
                            and isinstance(last_result.get("message"), str)
                            and last_result["message"].strip()):
                        current.response = last_result["message"]
                    else:
                        current.response = factual_fallback("go2", current.status, current)
                    current.reply_source = "runtime"
            publish_allowed = not current.parent_run_id or time.time() < current.deadline
            if not publish_allowed:
                current.response = ""
                current.reply_source = "runtime"
            elif publish_reply and not context.session_id.startswith("peer_"):
                if current.response:
                    current.reply_event_id = stable_reply_event_id(
                        current.run_id, session_id=event_session,
                        space_id=context.space_id)
                    publish_agent_event(
                        "agent_reply", agent_id="go2", session_id=event_session,
                        scope="space" if autonomous else "session", space_id=context.space_id,
                        status=_result(current)["result"], run_id=current.run_id,
                        trace_id=current.trace_id or current.run_id,
                        parent_run_id=current.parent_run_id, source=event_source,
                        text=current.response, reply_kind="final",
                        event_id=current.reply_event_id)
                else:
                    publish_agent_event(
                        "reply_error", agent_id="go2", session_id=event_session,
                        scope="space" if autonomous else "session", space_id=context.space_id,
                        status=_result(current)["result"], run_id=current.run_id,
                        trace_id=current.trace_id or current.run_id,
                        parent_run_id=current.parent_run_id, source=event_source)
            return current

        declarations = tool_schemas(context)
        if allowed_tools is None and context.origin_kind == "user_delegated":
            from cloud.agents.common.training.tools import SCHEMAS as training_schemas
            declarations.update(training_schemas)
        if allowed_tools is not None:
            declarations = {name: schema for name, schema in declarations.items()
                            if name in allowed_tools}
        try:
            await run_role_graph(
                run, model=get_text_llm(), tool_schemas=declarations,
                build_messages=build_messages, execute=execute, finalize=finalize,
                on_step=on_step, on_progress=progress,
                effectful=lambda name: name not in READ_ONLY_TOOLS and name != "list_training",
                initial_tool=initial_tool, resume=resume,
                terminal_tools=frozenset({"go2_navigate_to_visible_target", "go2_find_object"}),
                resume_value=message if resume else None, checkpointer=view.checkpointer)
        except asyncio.CancelledError:
            progress_closed.set()
            await view.reconcile_interrupted(session_id, cancelled=run)
            raise
        finally:
            if interaction_token is not None:
                go2.runtime.finish_life(interaction_token)
            if (context.origin_kind != 'agent_autonomous' and not motion_unconfirmed
                    and not asyncio.current_task().cancelling()):
                from cloud.go2.mode.free_explore import drive
                drive.resume_after_user()

    progress_closed.set()
    progress("finished", None)
    if (run.status == "needs_clarification" and publish_reply
            and not context.session_id.startswith("peer_")):
        run.reply_event_id = stable_reply_event_id(
            run.run_id, session_id=event_session,
            space_id=context.space_id, reply_kind="clarification")
        publish_agent_event(
            "agent_reply", agent_id="go2", session_id=event_session,
            scope="space" if autonomous else "session", space_id=context.space_id,
            status=run.status, run_id=run.run_id, trace_id=run.trace_id or run.run_id,
            parent_run_id=run.parent_run_id, source=event_source,
            text=run.response, reply_kind="clarification",
            event_id=run.reply_event_id)
    if run.status != "needs_clarification":
        if run.response and not context.session_id.startswith("peer_"):
            dialogue = run.goal
            if supplemental:
                dialogue += "\n用户补充：" + supplemental
            try:
                episode_memory.add_dialogue(run.run_id, dialogue, run.response, context)
            except Exception:
                logger.exception("Go2 dialogue memory write failed run=%s", run.run_id)
        view.pin(run.run_id, session_id)
    result = _result(run)
    if result["response"] and publish_reply:
        emit_agent_event("reply.emitted", trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, agent_id="go2", session_id=session_id,
                         reply_kind="final", channel="http", reply=result["response"])
    return result
