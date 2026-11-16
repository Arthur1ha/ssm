"""One sequential LangGraph tool cycle shared by steward, lamp, and Go2.

The graph validates and executes tool calls in response order, checkpointing
each receipt before continuing. Physical tools never run concurrently.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
import time
import uuid
from copy import deepcopy
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Callable, Literal, TypedDict

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger, use_effect
from cloud.agents.common.emotions import EmotionModel, EMOTION_INSTRUCTIONS
from cloud.agents.common.conversation import dialogue_turn, legacy_dialogue, trim_dialogue
from cloud.agents.common.messages import context_message, observed_tool_messages
from cloud.agents.common.outcomes import completed_status, outcome_summary
from cloud.agents.common.tool_results import UNCERTAIN, task_result
from cloud.observability import emit_agent_event

@dataclass
class GraphRun:
    run_id: str
    session_id: str
    goal: str
    deadline: float
    steps: list[dict] = field(default_factory=list)
    status: str = "executing"
    response: str = ""
    question: str = ""
    trace_id: str = ""
    parent_run_id: str = ""
    agent_id: str = ""
    origin_kind: str = ""
    reply_source: str = "runtime"
    reply_ready: bool = True
    decision_draft: str = ""
    reply_event_id: str = ""
    attempt: int = 0
    supplement: str = ""
    initial_context: dict = field(default_factory=dict)
    decision_space: dict = field(default_factory=dict)
    emotion: str = "neutral"
    emotion_step_id: str = ""
    model_turn: int = 0


class _State(TypedDict):
    run: dict
    decision: dict
    turns: int
    effects: int
    pending_calls: list[dict]
    messages: Annotated[list[AnyMessage], add_messages]


class DialogueResponse(EmotionModel):
    """A conversational decision; human input is handled by the Graph itself."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["final", "question"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _schema(name: str, declaration: Any) -> dict:
    schema = deepcopy(convert_to_openai_tool(declaration))
    schema["function"]["name"] = name
    properties = schema["function"]["parameters"].setdefault("properties", {})
    properties.setdefault("decision_reason", {"type": "string", "maxLength": 400,
        "description": "可选的简短公开选择说明，依据本次实际事实；不写私有推理。"})
    properties.setdefault("evidence_refs", {"type": "array", "maxItems": 8, "items": {"type": "string"},
        "description": "可选引用本轮 decision_evidence 的 ref；不能自行编造观察。"})
    properties["final"] = {
        "type": "boolean",
        "description": "成功后是否结束本次任务。单个动作或整批最后一步默认 true；"
                       "明确还需观察结果并继续其他步骤时填写 false。此字段不传给设备。",
    }
    return schema


async def _await(value):
    return await value if inspect.isawaitable(value) else value


async def invoke_blocking(callback, *args, **kwargs):
    """Run a synchronous adapter without blocking the graph or losing trace context."""
    outcome: Future = Future()
    context = contextvars.copy_context()

    def run():
        try:
            outcome.set_result(context.run(callback, *args, **kwargs))
        except BaseException as exc:
            outcome.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    while not outcome.done():
        await asyncio.sleep(0.05)
    return outcome.result()


async def _invoke(callback, *args):
    if inspect.iscoroutinefunction(callback):
        return await callback(*args)
    return await _await(await invoke_blocking(callback, *args))


async def run_role_graph(
    run: GraphRun,
    *,
    model: Any,
    tool_schemas: dict[str, Any],
    build_messages: Callable[[GraphRun], Any],
    execute: Callable[[str, dict, str], Any],
    record: Callable[[GraphRun, dict | None], None] | None = None,
    on_step: Callable[[dict], None] | None = None,
    on_progress: Callable[[str, dict | None], None] | None = None,
    effectful: Callable[[str], bool] = lambda _name: True,
    max_turns: int = 12,
    max_effects: int = 6,
    checkpointer: FileCheckpointSaver | None = None,
    effect_ledger: FileEffectLedger | None = None,
    reconcile_effect: Callable[[str, dict, str], Any] | None = None,
    finalize: Callable[[GraphRun], Any] | None = None,
    prepare_emotion: Callable[[GraphRun], Any] | None = None,
    resume: bool = False,
    resume_value: str | None = None,
    initial_tool: tuple[str, dict] | None = None,
    terminal_tools: frozenset[str] = frozenset(),
) -> GraphRun:
    """Run a role's model and domain executor through a durable StateGraph.

    `build_messages` reads current personality and device state each turn.
    `execute` alone owns authorization and actual device receipts. The native
    AIMessage tool call ID is the stable task/step ID for resume reconciliation.
    """
    if not run.agent_id:
        raise ValueError("GraphRun.agent_id is required")
    tools = [_schema(name, value) for name, value in tool_schemas.items()]
    bound_model = model.bind_tools(tools, parallel_tool_calls=False) if tools else model
    # Request JSON mode and let LangChain validate structured decisions.
    # OpenAI-compatible providers may still return ordinary final prose.
    # Passing this through the SDK's auto-parser would require strict schemas
    # for every business tool, changing their existing optional parameters.
    if hasattr(bound_model, "bind"):
        bound_model = bound_model.bind(extra_body={"response_format": {"type": "json_object"}})
    response_parser = PydanticOutputParser(pydantic_object=DialogueResponse)
    text_parser = StrOutputParser()
    response_policy = SystemMessage(content=(
        "通过原生 tool calling 调用业务工具；调用时不输出面向用户的文字。"
        "可在同次调用的 decision_reason 给出简短公开说明，并以 evidence_refs 引用本轮真实证据。"
        "没有必要说明时省略；不输出私有推理，不为闲聊解释思考过程。"
        "同轮返回的多个调用会严格按返回顺序串行执行，每步确认成功后才执行下一步。"
        "仅在各步参数都已明确时一次返回多个调用；若后续参数或是否执行取决于前一步结果，"
        "先只调用前一步，观察真实结果后再决定下一步。不重复成功的动作。"
        "工具参数 final 声明成功后是否结束本次任务；单个动作或整批最后一个动作默认结束。"
        "还需根据结果继续原目标时，当前调用必须明确 final=false；查询默认继续。"
        "工具结果 final=true 后运行直接结束，不再规划或执行后续调用。"
        "accepted 与 ok 都按动作成功处理，不为等待另一种成功回执重复执行。"
        "缺少必要信息时返回 JSON：{\"kind\":\"question\",\"text\":\"一个具体问题\"}，"
        "运行图将暂停等待回答。已有信息足够时不要反复确认。"
        "无需工具或目标完成时返回 JSON：{\"kind\":\"final\",\"text\":\"以当前人格直接回应\"}。"
        "闲聊、反问或可选建议不属于必须等待回答的追问。" + EMOTION_INSTRUCTIONS
    ))
    saver = checkpointer or FileCheckpointSaver()
    ledger = effect_ledger or FileEffectLedger()
    # Root graphs persist in LangGraph's default namespace. The run ID stays in
    # Graph state and in the API route index; it is not a checkpoint namespace.
    config = {"configurable": {"thread_id": thread_id_for(run.agent_id, run.session_id)}}
    thread_id = config["configurable"]["thread_id"]

    def emit(event: str, **fields: object) -> None:
        emit_agent_event(event, trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, parent_run_id=run.parent_run_id or None,
                         agent_id=run.agent_id, session_id=run.session_id, **fields)

    def sync_outer(current: GraphRun) -> None:
        """Keep legacy event and journal callbacks in sync with graph state."""
        run.__dict__.update(asdict(current))

    async def model_node(state: _State) -> dict:
        current = GraphRun(**state["run"])
        turns = state["turns"] + 1
        current.model_turn = turns
        sync_outer(current)
        emit("graph.node", node="model", phase="entered", turn=turns)
        if time.time() >= current.deadline:
            current.status, current.response = "timeout", "任务已到期；未确认的动作不会自动重发。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if turns > max_turns:
            current.status, current.response = "blocked", "模型轮次已达上限；其余步骤未执行。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if on_progress:
            on_progress("considering" if current.steps else "planning", None)
        messages = await _await(build_messages(current))
        history = trim_dialogue(state.get("messages", []), include_pending=True)
        # Clarifications belong to this run's live dialogue, not completed
        # historical requests. The role builder's goal/latest supplement are
        # already present in this checkpoint transcript.
        active = [message for message in history
                  if (message.id or "").startswith(current.run_id + ":")]
        if active:
            history = [message for message in history if message not in active]
            messages = [message for message in messages
                        if not (isinstance(message, HumanMessage) and message.name is None)]
            messages.extend(active)
        if history:
            # Keep role policy first and the current request last. History is
            # conversational context, never a receipt or permission to act.
            prefix = 0
            while prefix < len(messages) and isinstance(messages[prefix], SystemMessage):
                prefix += 1
            messages[prefix:prefix] = [SystemMessage(content=(
                "以下是同一智能体、同一会话的历史对话，用于理解偏好和承接语。"
                "历史回复不是当前设备状态、执行回执或新的动作授权；以本轮真实观测为准。"
            )), *history]
        outcomes = outcome_summary(current.steps)
        if outcomes["recovered_attempts"]:
            messages.append(context_message({"outcomes": outcomes}))
        messages.extend(observed_tool_messages(step for step in current.steps[-8:]
                                              if step.get("dispatch_status") == "observed"))
        messages.insert(0, response_policy)
        emit("model.request", turn=turns, input=messages)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(bound_model.ainvoke(messages),
                                              timeout=max(0.1, current.deadline - time.time()))
        except Exception as exc:
            emit("model.error", turn=turns, error=type(exc).__name__, message=str(exc),
                 cause=type(exc.__cause__).__name__ if exc.__cause__ else None,
                 cause_message=str(exc.__cause__) if exc.__cause__ else None)
            current.status, current.response = "error", "暂时无法可靠理解下一步；已执行的动作不会重发。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        calls = getattr(response, "tool_calls", None) or []
        content = getattr(response, "content", "")
        emit("model.response", turn=turns, output={"content": content, "tool_calls": calls},
             duration_ms=round((time.monotonic() - started) * 1000))
        if (not calls and not getattr(response, 'invalid_tool_calls', None)
                and (not isinstance(content, str) or not content.strip())
                and time.time() < current.deadline - 1):
            emit("model.empty_retry", turn=turns)
            try:
                response = await asyncio.wait_for(bound_model.ainvoke(messages + [SystemMessage(
                    content="上一轮没有返回内容或工具调用。请依据已有结果回答；"
                            "目标尚未完成时才调用必要工具，不重复已成功的动作。"
                )]), timeout=max(0.1, current.deadline - time.time()))
            except Exception as exc:
                emit("model.error", turn=turns, error=type(exc).__name__, message=str(exc))
            else:
                calls = getattr(response, "tool_calls", None) or []
                content = getattr(response, "content", "")
                emit("model.response", turn=turns, retry=True,
                     output={"content": content, "tool_calls": calls},
                     duration_ms=round((time.monotonic() - started) * 1000))
        if getattr(response, "invalid_tool_calls", None):
            current.status, current.response = "error", "工具调用格式无效，未执行新动作。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if not calls:
            if not isinstance(content, str) or not content.strip():
                current.status, current.response = "error", "模型返回空内容，未执行新动作。"
            else:
                try:
                    dialogue = response_parser.invoke(response)
                except OutputParserException as exc:
                    if content.lstrip().startswith(("{", "[", "```")):
                        emit("model.error", turn=turns, error=type(exc).__name__, message=str(exc))
                        current.status, current.response = "error", "模型回复格式无效，未执行新动作。"
                        return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
                    # Plain prose can only end this turn. It cannot dispatch
                    # tools, resume an action, or create a human-input interrupt.
                    dialogue = DialogueResponse(kind="final", text=text_parser.invoke(response))
                current.emotion = dialogue.emotion
                if dialogue.kind == "question":
                    current.reply_source = "decision_model"
                    transcript = list(state.get("messages", []))
                    if not active:
                        transcript.append(HumanMessage(content=current.goal,
                                                       id=f"{current.run_id}:user"))
                        # Compatibility for a run paused before clarification
                        # exchanges were added to the messages channel.
                        if current.supplement:
                            transcript.append(HumanMessage(content=current.supplement,
                                id=f"{current.run_id}:clarification:legacy"))
                    transcript.append(AIMessage(content=dialogue.text,
                        id=f"{current.run_id}:clarification:question:{turns}"))
                    return {"run": asdict(current),
                            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES),
                                         *trim_dialogue(transcript)],
                            "decision": {"kind": "ask", "question": dialogue.text[:500]}, "turns": turns}
                current.status = completed_status(current.steps)
                current.decision_draft = dialogue.text
                current.response = dialogue.text
                current.reply_source = "decision_model"
                emit("reply.draft", turn=turns, reply=current.decision_draft)
                # Only a current conversational decision may select a saved
                # expression. Closing replies and replayed events never act.
                if (prepare_emotion is not None and "apply_training" in tool_schemas
                        and current.status == "ok" and current.emotion != "neutral"
                        and not current.emotion_step_id):
                    try:
                        expression = await asyncio.wait_for(
                            _invoke(prepare_emotion, current),
                            timeout=max(0.1, current.deadline - time.time()))
                    except Exception as exc:
                        emit("emotion.skipped", reason="training_unavailable",
                             error=type(exc).__name__)
                    else:
                        if expression is not None:
                            current.emotion_step_id = current.run_id + ":emotion"
                            current.status = "executing"
                            return {"run": asdict(current), "turns": turns,
                                    "decision": {"kind": "tool"}, "pending_calls": [{
                                        "name": "apply_training", "args": expression,
                                        "id": current.emotion_step_id}]}
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        # Persist native call IDs before dispatch, including IDs omitted by a
        # compatible provider. Each tool node consumes exactly one queued call.
        pending = [{**call, "id": call.get("id") or uuid.uuid4().hex}
                   if isinstance(call, dict) else call for call in calls]
        return {"run": asdict(current), "decision": {"kind": "tool"},
                "pending_calls": pending, "turns": turns}

    async def model_route(state: _State) -> str:
        return state["decision"]["kind"]

    async def tool_node(state: _State) -> dict:
        current = GraphRun(**state["run"])
        pending = state.get("pending_calls") or []
        # Existing checkpoints may still contain a single prepared decision.
        if not pending:
            decision = state["decision"]
            pending = [{"name": decision["name"], "args": decision["params"],
                        "id": decision["step_id"]}]
        call, remaining = pending[0], pending[1:]
        expression = (bool(current.emotion_step_id) and isinstance(call, dict)
                      and call.get("id") == current.emotion_step_id)

        def stop(status: str, text: str) -> dict:
            current.status, current.response = status, text
            if expression:
                current.decision_draft = ""
            return {"run": asdict(current), "decision": {"kind": "finish"},
                    "pending_calls": []}

        if time.time() >= current.deadline:
            return stop("timeout", "任务已到期；其余步骤未执行，未确认的动作不会自动重发。")
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("args"), dict):
            return stop("error", "工具调用格式无效，未执行新动作。")
        name = call["name"]
        if name not in tool_schemas:
            return stop("rejected", "模型请求了未授权工具，未执行新动作。")
        declaration = tool_schemas[name]
        args = dict(call["args"])
        final = args.pop("final", not remaining and effectful(name))
        if type(final) is not bool:
            return stop("rejected", "任务终结标记必须为布尔值，未执行新动作。")
        # Display metadata never changes the business effect fingerprint. Existing
        # schema-owned evidence remains available to mandatory game validation.
        reason, refs = args.get("decision_reason"), args.get("evidence_refs")
        own_fields = (declaration.model_fields if inspect.isclass(declaration)
                      and issubclass(declaration, BaseModel) else
                      (declaration.get("function", declaration).get("parameters", {}).get("properties", {})
                       if isinstance(declaration, dict) else {}))
        for field_name in ("decision_reason", "evidence_refs"):
            if field_name not in own_fields:
                args.pop(field_name, None)
        basis = None
        if reason is not None or refs is not None:
            from cloud.agents.common.progress_facts import resolve_public_decision
            try:
                basis = resolve_public_decision(current.decision_space, reason, refs)
            except (ValueError, TypeError):
                emit("decision.explanation_discarded", tool=name, reason="invalid_public_metadata")
                # Mandatory fields are validated by their original business
                # executor; optional malformed display metadata is discarded.
                for field_name in ("decision_reason", "evidence_refs"):
                    field = own_fields.get(field_name)
                    if field_name in args and (field is None or
                            hasattr(field, "is_required") and not field.is_required()):
                        # Game invitation evidence remains a business precondition.
                        if name != "send_message" or args.get("interaction_kind") != "game_invite":
                            args.pop(field_name, None)
        if inspect.isclass(declaration) and issubclass(declaration, BaseModel):
            try:
                args = declaration.model_validate(args).model_dump(exclude_none=True)
            except ValidationError:
                return stop("rejected", "工具参数未通过校验，未执行新动作。")
        if effectful(name) and state["effects"] >= max_effects:
            return stop("blocked", "任务动作次数已达上限；其余步骤未执行。")
        execute_params = args
        params = {key: value for key, value in args.items()
                  if key not in {"decision_reason", "evidence_refs"}}
        step_id = call["id"]
        emit("graph.node", node="tool", phase="entered", step_id=step_id, tool=name)
        is_effect = effectful(name)
        step = {"step_id": step_id, "tool_call_id": step_id, "tool": name,
                "params": params, "effectful": is_effect, "final": final,
                "dispatch_status": "pending", "result": None,
                "model_turn": current.model_turn,
                "sequence": current.model_turn * 1000 + (len(current.steps) + 1) * 10,
                "ts": time.time()}
        if basis:
            step["decision_basis"] = basis
        current.steps.append(step)
        sync_outer(current)
        if record:
            record(current, step)
        if on_progress:
            on_progress("decision", step)
            on_progress("dispatching", step)
        emit("tool.dispatch", step_id=step_id, tool_call_id=step_id, tool=name, params=params,
             dispatch_status="pending", effectful=is_effect)
        started = time.monotonic()
        try:
            if is_effect:
                reservation = ledger.reserve(current.agent_id, thread_id, current.run_id,
                                             step_id, name, params)
                emit("effect.reserved" if reservation["reservation"] == "new" else "effect.reused",
                     step_id=step_id, tool=name, status=reservation["status"])
                if reservation["reservation"] == "existing":
                    result = reservation.get("result")
                    if result is None and reconcile_effect is not None:
                        result = await _invoke(reconcile_effect, name, params, step_id)
                    if result is None:
                        result = {"result": "unknown", "error": "prior_dispatch_unconfirmed"}
                else:
                    with use_effect(ledger, (current.agent_id, thread_id, current.run_id, step_id)):
                        result = await asyncio.wait_for(_invoke(execute, name, execute_params, step_id),
                                                        timeout=max(0.1, current.deadline - time.time()))
                if isinstance(result, dict) and result.get("result") not in {
                    "unknown", "timeout", "executing"
                }:
                    ledger.mark_result(current.agent_id, thread_id,
                                       current.run_id, step_id, result)
            else:
                result = await asyncio.wait_for(_invoke(execute, name, execute_params, step_id),
                                                timeout=max(0.1, current.deadline - time.time()))
        except asyncio.TimeoutError:
            result = {"result": "timeout", "error": "run_deadline_exceeded"}
        except asyncio.CancelledError:
            step.update(dispatch_status="unknown", result={"result": "cancelled", "error": "execution_cancelled"})
            if record:
                record(current, step)
            raise
        except Exception as exc:
            result = {"result": "unknown" if is_effect else "error",
                      "error": type(exc).__name__}
        result = task_result(result, final=final)
        if name in terminal_tools or expression:
            result["final"] = True
        if expression and result["result"] != "ok":
            # A pre-action conversational draft cannot describe an unconfirmed
            # expression. Finish with receipt-grounded speech, never replan.
            current.decision_draft = ""
        step.update(dispatch_status="observed", result=result)
        sync_outer(current)
        if record:
            record(current, step)
        if on_step:
            on_step(step)
        if on_progress:
            on_progress("observed", step)
        emit("tool.result", step_id=step_id, tool=name, status=result["result"], result=result,
             duration_ms=round((time.monotonic() - started) * 1000))
        if result["result"] in UNCERTAIN:
            current.status, current.response = result["result"], "请求已发送，但实际完成情况尚未确认；后续步骤已暂停。"
        elif result["result"] == "cancelled":
            current.status, current.response = "cancelled", "任务已取消；后续步骤未执行。"
        elif result["final"]:
            current.status = (completed_status(current.steps) if result["result"] == "ok"
                              else result["result"])
            current.response = result.get("message") or result.get("reason") or result.get("error") or result["result"]
        if result["final"] or result["result"] in UNCERTAIN:
            route = "finish"
        elif result["result"] == "ok" and remaining:
            route = "tool"
        else:
            # Failed calls invalidate the rest of this model response. Let the
            # existing model node observe the receipt and decide how to proceed.
            route = "model"
        return {"run": asdict(current), "effects": state["effects"] + int(is_effect),
                "decision": {"kind": route},
                "pending_calls": remaining if route == "tool" else []}

    async def tool_route(state: _State) -> str:
        return state["decision"]["kind"]

    async def start_route(state: _State) -> str:
        return "tool" if state.get("decision", {}).get("kind") == "tool" else "model"

    async def finalize_node(state: _State) -> dict:
        current = GraphRun(**state["run"])
        sync_outer(current)
        emit("graph.node", node="finalize", phase="entered", status=current.status)
        if finalize is not None:
            updated = await _await(finalize(current))
            if isinstance(updated, GraphRun):
                current = updated
        history = state.get("messages", [])
        if any((message.id or "").startswith(current.run_id + ":") for message in history):
            answer = current.response or current.decision_draft
            turn = ([AIMessage(content=answer, id=f"{current.run_id}:assistant")]
                    if answer else [])
        else:
            turn = dialogue_turn(current)
        history = trim_dialogue([*history, *turn])
        return {"run": asdict(current),
                "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *history]}

    async def ask_node(state: _State) -> dict:
        question = state["decision"]["question"]
        answer = interrupt({"question": question, "run_id": state["run"]["run_id"]})
        current = GraphRun(**state["run"])
        current.question = ""
        current.supplement = str(answer)
        current.status = "executing"
        transcript = list(state.get("messages", []))
        if not any((message.id or "").startswith(current.run_id + ":") for message in transcript):
            transcript.extend([
                HumanMessage(content=current.goal, id=f"{current.run_id}:user"),
                AIMessage(content=question,
                          id=f"{current.run_id}:clarification:question:{state['turns']}")])
        history = trim_dialogue([*transcript,
            HumanMessage(content=str(answer),
                         id=f"{current.run_id}:clarification:answer:{state['turns']}")],
            include_pending=True)
        return {"run": asdict(current),
                "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *history]}

    graph = StateGraph(_State)
    graph.add_node("model", model_node)
    graph.add_node("tool", tool_node)
    graph.add_node("ask", ask_node)
    graph.add_node("finalize", finalize_node)
    graph.add_conditional_edges(START, start_route, {"tool": "tool", "model": "model"})
    graph.add_conditional_edges("model", model_route, {"tool": "tool", "ask": "ask", "finish": "finalize"})
    graph.add_conditional_edges("tool", tool_route, {"tool": "tool", "model": "model", "finish": "finalize"})
    graph.add_edge("ask", "model")
    graph.add_edge("finalize", END)
    compiled = graph.compile(checkpointer=saver)
    emit("run.resumed" if resume else "run.started", goal=run.goal)
    async with saver.async_run_lock(thread_id):
        if resume:
            output = await compiled.ainvoke(
                Command(resume=resume_value if resume_value is not None else run.supplement),
                config, durability="sync")
        else:
            first = {}
            if initial_tool is not None:
                name, params = initial_tool
                if name not in tool_schemas:
                    raise ValueError("initial_tool_not_allowed")
                first = {"kind": "tool", "name": name, "params": params,
                         "step_id": f"{run.run_id}:initial"}
            head = saver.get_tuple(config)
            values = head.checkpoint.get("channel_values", {}) if head else {}
            seed = (legacy_dialogue(saver, config, run.run_id)
                    if "messages" not in values else [])
            output = await compiled.ainvoke({"run": asdict(run), "decision": first,
                                             "pending_calls": [],
                                             "messages": seed,
                                             "turns": len(run.steps),
                                             "effects": sum(bool(s.get("effectful")) for s in run.steps)},
                                            config, durability="sync")
    if "__interrupt__" in output:
        run.__dict__.update(output["run"])
        payload = output["__interrupt__"][0].value
        run.status, run.question, run.response = "needs_clarification", payload["question"], payload["question"]
        run.reply_source = "decision_model"
        emit("run.paused", reason="human_input", question=run.question)
    else:
        updated = GraphRun(**output["run"])
        run.__dict__.update(updated.__dict__)
    if record:
        record(run, None)
    if on_progress:
        on_progress("summarizing", None)
    emit("run.finished", status=run.status, response=run.response)
    return run
