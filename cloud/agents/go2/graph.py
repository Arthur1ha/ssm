"""Go2's LangGraph conversation entry; device executor remains authority."""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
import asyncio
from dataclasses import replace

from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.common.emotions import normalize_emotion
from cloud.agents.common.models import async_model_clients
from cloud.agents.go2.run_view import Go2RunView, stable_reply_event_id
from cloud.agents.go2.prompts import build_messages as go2_messages, persona_reply_prompt
from cloud.agents.go2.workflows.search import SEARCH_SERVER_TIMEOUT_S
from cloud.agents.go2.tools.declarations import READ_ONLY_TOOLS, tool_schemas
from cloud.agents.common.replies import finalize_reply, factual_fallback, public_reply_text
from cloud.agents.common.messages import context_message
from cloud.observability import bind_context, emit_agent_event
from cloud.go2.models import get_text_llm
from cloud.go2.tools.executor import execute_tool
from cloud.go2.tools.registry import TOOL_REGISTRY
from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.space.execution import ExecutionContext
from cloud.agents.go2.memory.episode import episode_memory
from cloud.agents.go2.memory.recall import recall_memory

_view: Go2RunView | None = None
logger = logging.getLogger(__name__)


def get_store() -> Go2RunView:
    """Return the checkpoint-backed view used by task status and recovery."""
    global _view
    if _view is None:
        _view = Go2RunView()
    return _view


def _result(run: GraphRun) -> dict:
    steps = [{"tool": s["tool"], "params": s.get("params") or {},
              "effectful": s.get("effectful", s["tool"] not in READ_ONLY_TOOLS),
              "result": s.get("result") or {"result": "unknown"},
              "step_id": s["step_id"]} for s in run.steps]
    status = run.status
    if status == "partial":
        status = next((s["result"]["result"] for s in reversed(steps)
                       if s["result"]["result"] != "ok"), "error")
    return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
            "result": status, "response": run.response,
            "emotion": normalize_emotion(run.emotion), "reply_source": run.reply_source,
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
                       publish_reply: bool = True, selection: dict | None = None,
                       task_adapter=None) -> dict:
    view = get_store()
    async with view.index.async_session_lock("go2", session_id, goal_revision=context.goal_revision):
        return await _run_go2_loop_locked(
            session_id, message, context=context, view=view,
            skill_id=skill_id, params=params, run_id=run_id, deadline=deadline,
            on_step=on_step, resume=resume, trace_id=trace_id,
            parent_run_id=parent_run_id, allowed_tools=allowed_tools,
            tool_guard=tool_guard, task_context=task_context,
            publish_reply=publish_reply, selection=selection, task_adapter=task_adapter)


@async_model_clients()
async def _run_go2_loop_locked(session_id: str, message: str, *, context: ExecutionContext,
                               view: Go2RunView, skill_id: str | None,
                               params: dict | None, run_id: str | None,
                               deadline: float | None, on_step, resume: bool,
                               trace_id: str | None, parent_run_id: str | None,
                               allowed_tools: frozenset[str] | None,
                               tool_guard, task_context, publish_reply: bool, selection: dict | None,
                               task_adapter=None) -> dict:
    # Resolve public requests before registering a run or claiming device work.
    # CHAT is an explicit conversation entry, not a missing hardware tool.
    initial_tool = task_adapter.initial_tool if task_adapter is not None else None
    if task_adapter is not None:
        allowed_tools = task_adapter.allowed_tools
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

    if context.training_ref and context.training_step_index is None:
        if skill_id not in (None, 'go2_chat') or allowed_tools is not None or tool_guard is not None:
            return {'result': 'rejected', 'error': 'training_not_allowed_for_restricted_task'}
        # A trusted HTTP delegation references the whole behavior. Individual
        # transitions stay inside the API process and never recurse through
        # another delegated conversation.
        initial_tool = ('apply_training', {
            'unit_id': 'go2', 'teaching_id': context.training_ref,
            'revision': context.training_revision,
            'overrides': context.training_overrides or {}})

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
                  min(deadline or time.time() + SEARCH_SERVER_TIMEOUT_S,
                      context.expires_at or time.time() + SEARCH_SERVER_TIMEOUT_S))
        if view.session_busy(run.run_id, session_id):
            return {"result": "rejected", "error": "session_busy", "run_id": run.run_id}
        run.trace_id = trace_id or "tr_" + uuid.uuid4().hex
        run.parent_run_id = parent_run_id or ""
        run.agent_id = "go2"
        run.origin_kind = context.origin_kind
        run.goal_revision = context.goal_revision
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

    # The coordinating run owns the single device reply for this user turn.
    # Keep ordinary device chats and autonomous runs on their existing path.
    delegated = bool(run.parent_run_id) and context.origin_kind == "user_delegated"
    publish_reply = publish_reply and not delegated


    def build_messages(current: GraphRun):
        from cloud.agents.go2.personality.profile import get_profile
        profile = get_profile()
        remembered = recall_memory(
            current.goal + (" " + supplemental if supplemental else ""),
            actor=context.actor, persist=context.persist, session_id=session_id,
            episode_store=episode_memory)
        messages = go2_messages(current, supplement=supplemental,
                                memory=remembered.text if remembered.fragments else "",
                                space_id=context.space_id)
        if selection:
            messages.insert(1, context_message({"training_selection": selection}))
        if task_context is not None:
            messages.insert(1, context_message({"task_state": task_context()}))
        emit_agent_event("persona.loaded", trace_id=current.trace_id or current.run_id,
                         run_id=current.run_id, agent_id="go2", source="profile",
                         revision=profile.get("revision"),
                         content_hash=hashlib.sha256(profile["prompt"].encode()).hexdigest(),
                         prompt_trimmed=False)
        return task_adapter.messages(messages, current) if task_adapter is not None else messages

    def prepare_emotion(current: GraphRun):
        # Public actions and restricted tasks retain their exact tool scope.
        if (initial_tool is not None or allowed_tools is not None or tool_guard is not None
                or skill_id not in (None, 'go2_chat') or selection):
            return None
        from cloud.agents.common.training.context import emotion_training_call
        return emotion_training_call(current, space_id=context.space_id,
                                     origin_kind=context.origin_kind)

    interaction_token = None
    motion_unconfirmed = False
    runtime = runtime_for(go2)
    training_availability = None
    if context.origin_kind == "agent_autonomous":
        from cloud.go2.mode.free_explore import drive
        generation, owner = drive._generation, drive.owner_session_id
        activity_id = drive._activity_id()
        epoch = runtime.data.get("epoch") if runtime is not None else None

        def training_availability(_trusted):
            latest = runtime_for(go2)
            return bool(latest is not None and go2.is_connected
                        and latest.data.get("epoch") == epoch
                        and latest.data.get("autonomy_mode") == "free_explore"
                        and not latest.active_reason()
                        and generation == drive._generation and owner == drive.owner_session_id
                        and activity_id == drive._activity_id()
                        and not drive.user_interrupt and not drive._pending_question_id)
    # Explicit read/life skills keep status lookup and wake reachable without
    # claiming an interaction. Other accepted Graph tasks own play until their
    # true terminal or clarification event, including time spent waiting on LLM.
    read_or_life = initial_tool is not None and (
        initial_tool[0] in READ_ONLY_TOOLS or initial_tool[0] in {'go2_life', 'apply_training'}
        or (initial_tool[0] in TOOL_REGISTRY and TOOL_REGISTRY[initial_tool[0]].motion))
    if runtime is not None and not runtime.active_reason() and not read_or_life:
        if context.origin_kind != 'agent_autonomous':
            from cloud.go2.service import prepare_user_interaction
            try:
                await prepare_user_interaction(context)
            except ValueError as exc:
                return {'result': 'blocked', 'error': str(exc), 'run_id': run.run_id}
        if runtime.life_task_available:
            interaction_token = runtime.begin_life('play', context.session_id, run.run_id)

    training_attempt_start = len(run.steps)

    async def execute(name: str, arguments: dict, step_id: str) -> dict:
        from cloud.space.user_goals import get_goal_registry
        get_goal_registry().check(context.goal_revision)
        nonlocal interaction_token, motion_unconfirmed
        execution_context = context
        if name not in READ_ONLY_TOOLS and name != "submit_game_decision":
            from cloud.peers.turns import peer_turn_available
            if not peer_turn_available():
                return {"result": "cancelled", "error": "peer_context_superseded", "final": True}
        if task_adapter is not None:
            blocked = task_adapter.guard(name, arguments)
            if blocked is not None:
                return blocked
            submitted = task_adapter.submit(name, arguments)
            if submitted is not None:
                return submitted
        if name == "apply_training":
            if allowed_tools is not None or tool_guard is not None or skill_id not in (None, 'go2_chat'):
                return {'result': 'rejected', 'error': 'training_not_allowed_for_restricted_task'}
            from cloud.agents.common.training.tools import prepare_training_application
            from cloud.go2.service import run_training_behavior
            try:
                prepared = await prepare_training_application(arguments, context)
            except ValueError as exc:
                return {"result": "rejected", "error": str(exc)}
            if prepared["unit_id"] != "go2":
                return {"result": "rejected", "error": "training_target_mismatch"}
            if interaction_token is not None:
                runtime.finish_life(interaction_token)
                interaction_token = None
            result = await run_training_behavior(
                prepared, context, run_id=run.run_id + ':' + step_id,
                **({"availability": training_availability} if training_availability is not None else {}))
            motion_unconfirmed = result.get('result') in {'accepted', 'executing', 'unknown', 'timeout'}
            return result
        if name in {"list_training", "propose_training"}:
            from dataclasses import asdict
            from cloud.agents.common.training.tools import execute_training
            if allowed_tools is not None or skill_id or run.parent_run_id or session_id.startswith("peer_"):
                return {"result": "rejected", "error": "direct_user_request_required"}
            return await execute_training(name, arguments, {**asdict(context), "run_id": run.run_id,
                                                            "source_text": message, "selection": selection,
                                                            "proposal_attempts": run.steps[training_attempt_start:-1]})
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
            if not runtime.active_reason() and runtime.life_task_available:
                interaction_token = runtime.begin_life('play', context.session_id, run.run_id)
        # A life command is itself the requested task, so release this Graph's
        # completed interaction before evaluating idle-only rest.
        if name == 'go2_life' and interaction_token is not None:
            runtime.finish_life(interaction_token)
            interaction_token = None
        from cloud.agents.go2.workflows.search import search_request
        from cloud.go2.perception.cargo import cargo_request
        from cloud.go2.navigation.cargo_push import expect_cargo
        from contextlib import nullcontext
        original_target = cargo_request(run.goal) if name == 'go2_push_cargo' else nullcontext()
        cargo_reference = None
        if name == 'go2_push_cargo' and task_adapter is None:
            cargo_reference = next(((step.get('result') or {}).get('reference')
                for step in reversed(run.steps)
                if step.get('tool') == name and (step.get('result') or {}).get('reference')), None)
        with search_request(run.run_id, run.deadline), original_target, (
                expect_cargo(cargo_reference) if cargo_reference is not None else nullcontext()), (
                task_adapter.execution_context(name, arguments, step_id)
                if task_adapter is not None else nullcontext()):
            options = task_adapter.options(name, arguments, step_id) if task_adapter is not None else {}
            result = await execute_tool(name, arguments, context=execution_context, **options)
        if task_adapter is not None:
            result = task_adapter.transform(name, arguments, result)
        if TOOL_REGISTRY[name].motion:
            motion_unconfirmed = (result.get("result") in {"accepted", "executing", "unknown", "timeout"}
                                  or result.get("reason") == "stop_unconfirmed")
        if name in {"go2_sport", "go2_move", "go2_set_led", "go2_observe"}:
            result = {**result, "device_connected": bool(go2.is_connected),
                      "device_fsm": getattr(go2, "body_state", None)}
            emit_agent_event("device.result", trace_id=run.trace_id or run.run_id,
                             run_id=run.run_id, agent_id="go2", task_id=step_id,
                             status=result.get("result"), result=result,
                             device_connected=bool(go2.is_connected),
                             device_fsm=getattr(go2, "body_state", None),
                             device_snapshot_at=time.time(),
                             completion_basis=(result.get("confirmation") or "webrtc_result")
                             if result.get("result") == "ok" else "tool_boundary")
        return {**result, "physical_state": go2.state_snapshot()}

    with bind_context(trace_id=run.trace_id or run.run_id, run_id=run.run_id,
                      parent_run_id=run.parent_run_id or None, agent_id="go2",
                      session_id=session_id, origin_kind=run.origin_kind):
        from cloud.api.intents import publish_agent_event
        autonomous = context.origin_kind == "agent_autonomous"
        event_session = "" if autonomous else session_id
        event_source = "autonomous" if autonomous else (
            "delegated_skill" if run.parent_run_id else "device_chat")
        def progress(phase, step):
            # Stage submissions have no physical effect or public tool step.
            # Their internal runner events remain available for diagnostics.
            if task_adapter is not None and task_adapter.decision_schema is not None:
                return
            from cloud.agents.common.progress_facts import progress_event_facts
            publish_agent_event("agent_progress", session_id=event_session,
                scope="space" if autonomous else "session", space_id=context.space_id,
                source=event_source,
                **{**progress_event_facts(run, phase, step),
                   **(task_adapter.event_fields() if task_adapter is not None else {})})
        async def finalize(current: GraphRun) -> GraphRun:
            """Validate facts, speak as Go2 and checkpoint the user-visible reply."""
            if task_adapter is not None:
                if not task_adapter.available():
                    current.status, current.response = "cancelled", ""
                    return current
                task_adapter.settle_status(current)
                if task_adapter.finish_decision(current):
                    return current
            if delegated:
                current.response = public_reply_text(current.decision_draft) if initial_tool is None else ""
                current.reply_source = "decision_model" if current.response else "runtime"
                return current
            def fallback(value):
                last_step = value.steps[-1] if value.steps else {}
                last_result = last_step.get("result") or {}
                if (last_step.get("tool") in {"go2_navigate_to_visible_target", "go2_find_object", "go2_move"}
                        and isinstance(last_result.get("message"), str)
                        and last_result["message"].strip()):
                    return last_result["message"]
                return factual_fallback("go2", value.status, value)

            from cloud.agents.go2.personality.profile import get_profile
            last = current.steps[-1] if current.steps else {}
            receipt = last.get('result') or {}
            if (last.get('tool') == 'go2_push_cargo' and receipt.get('result') == 'ok'
                    and receipt.get('progress') == 'already_near_destination'
                    and receipt.get('motion_performed') is False
                    and isinstance(receipt.get('message'), str) and receipt['message'].strip()):
                # Observing an already-satisfied goal cannot become a story
                # about physical work. Project this explicit receipt directly.
                current.response = public_reply_text(receipt['message'])
                current.reply_source = 'tool_receipt'
                current.emotion = 'neutral'
                emit_agent_event('reply.validated', agent_id='go2', run_id=current.run_id,
                    session_id=current.session_id, trace_id=current.trace_id or current.run_id,
                    reply=current.response, validation='receipt', reason='already_near_destination',
                    reply_kind='final')
            else:
                await finalize_reply(current, model=get_text_llm,
                                     instructions=lambda value: persona_reply_prompt(value, get_profile())
                                         + ("\n" + task_adapter.instructions if task_adapter is not None else ""),
                                     fallback=fallback)
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
                        "agent_reply" if current.reply_source != "runtime" else "reply_error", agent_id="go2", session_id=event_session,
                        scope="space" if autonomous else "session", space_id=context.space_id,
                        status=_result(current)["result"], run_id=current.run_id,
                        trace_id=current.trace_id or current.run_id,
                        parent_run_id=current.parent_run_id, source=event_source,
                        text=current.response, emotion=current.emotion, reply_kind="final",
                        reply_source=current.reply_source,
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
        if allowed_tools is None:
            from cloud.agents.common.training.tools import SCHEMAS as training_schemas
            declarations['apply_training'] = training_schemas['apply_training']
            if context.origin_kind == "user_delegated":
                declarations.update(training_schemas)
        if allowed_tools is not None:
            declarations = {name: schema for name, schema in declarations.items()
                            if name in allowed_tools}
        if task_adapter is not None:
            declarations = task_adapter.declarations(declarations)
        try:
            await run_role_graph(
                run, model=get_text_llm(), tool_schemas=declarations,
                max_turns=None, max_effects=None, recursion_limit=10000,
                build_messages=build_messages, execute=execute, finalize=finalize,
                prepare_emotion=prepare_emotion,
                on_step=on_step, on_progress=progress,
                effectful=lambda name: name not in READ_ONLY_TOOLS and name not in {"list_training", "submit_game_decision"},
                initial_tool=initial_tool, resume=resume,
                terminal_tools=(frozenset({"submit_game_decision"}) if task_adapter is not None
                                and task_adapter.decision_schema is not None else
                                frozenset({initial_tool[0]} if delegated and initial_tool else set())),
                resume_value=message if resume else None, checkpointer=view.checkpointer)
        except asyncio.CancelledError:
            await view.reconcile_interrupted(session_id, cancelled=run)
            raise
        finally:
            if interaction_token is not None:
                go2.runtime.finish_life(interaction_token)
            if (context.origin_kind != 'agent_autonomous' and not motion_unconfirmed
                    and not asyncio.current_task().cancelling()):
                from cloud.go2.mode.free_explore import drive
                drive.resume_after_user()

    from cloud.space.user_goals import get_goal_registry
    if not get_goal_registry().is_current(run.goal_revision):
        run.status, run.response, run.question = "cancelled", "", ""
    progress("finished", None)
    if (run.status == "needs_clarification" and publish_reply
            and not context.session_id.startswith("peer_")):
        run.reply_event_id = stable_reply_event_id(
            run.run_id, session_id=event_session,
            space_id=context.space_id, reply_kind="clarification", model_turn=run.model_turn)
        publish_agent_event(
            "agent_reply", agent_id="go2", session_id=event_session,
            scope="space" if autonomous else "session", space_id=context.space_id,
            status=run.status, run_id=run.run_id, trace_id=run.trace_id or run.run_id,
            parent_run_id=run.parent_run_id, source=event_source,
            text=run.response, emotion=run.emotion, reply_kind="clarification",
            goal_revision=run.goal_revision,
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
