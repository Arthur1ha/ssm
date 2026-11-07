"""One sequential LangGraph tool cycle shared by steward, lamp, and Go2.

The graph gates every model response before dispatch. A response with multiple
tool calls executes none, so physical tools cannot run concurrently.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, TypedDict

from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ValidationError

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.observability import emit_agent_event

UNCERTAIN = {"accepted", "executing", "timeout", "unknown"}
FAILURE = {"blocked", "rejected", "cancelled", "error", "needs_clarification"}


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


class _State(TypedDict):
    run: dict
    decision: dict
    turns: int
    effects: int


def _schema(name: str, declaration: Any) -> dict:
    schema = convert_to_openai_tool(declaration)
    schema["function"]["name"] = name
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
        sync_outer(current)
        emit("graph.node", node="model", phase="entered", turn=turns)
        if time.time() >= current.deadline:
            current.status, current.response = "timeout", "任务已到期；未确认的动作不会自动重发。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if turns > max_turns:
            current.status, current.response = "blocked", "模型轮次已达上限；其余步骤未执行。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if on_progress:
            on_progress("considering" if current.steps else "planning", current.steps[-1] if current.steps else None)
        messages = await _await(build_messages(current))
        for step in current.steps[-8:]:
            if step.get("dispatch_status") != "observed":
                continue
            call_id = step.get("tool_call_id") or step.get("step_id")
            messages.append(AIMessage(content="", tool_calls=[{
                "name": step["tool"], "args": step.get("params") or {}, "id": call_id,
            }]))
            messages.append(ToolMessage(
                content=json.dumps(step.get("result") or {}, ensure_ascii=False, default=str),
                tool_call_id=call_id,
            ))
        emit("model.request", turn=turns, input=messages)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(bound_model.ainvoke(messages),
                                              timeout=max(0.1, current.deadline - time.time()))
        except Exception as exc:
            emit("model.error", turn=turns, error=type(exc).__name__, message=str(exc))
            current.status, current.response = "error", "暂时无法可靠理解下一步；已执行的动作不会重发。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        calls = getattr(response, "tool_calls", None) or []
        content = getattr(response, "content", "")
        emit("model.response", turn=turns, output={"content": content, "tool_calls": calls},
             duration_ms=round((time.monotonic() - started) * 1000))
        if not calls and (not isinstance(content, str) or not content.strip()) and time.time() < current.deadline - 1:
            emit("model.empty_retry", turn=turns)
            try:
                response = await asyncio.wait_for(bound_model.ainvoke(messages + [HumanMessage(
                    content="上一轮没有返回内容或工具调用。请根据原请求调用一个合适的工具，或明确回答。"
                )]), timeout=max(0.1, current.deadline - time.time()))
            except Exception as exc:
                emit("model.error", turn=turns, error=type(exc).__name__, message=str(exc))
            else:
                calls = getattr(response, "tool_calls", None) or []
                content = getattr(response, "content", "")
                emit("model.response", turn=turns, retry=True,
                     output={"content": content, "tool_calls": calls},
                     duration_ms=round((time.monotonic() - started) * 1000))
        if len(calls) > 1:
            current.status, current.response = "blocked", "模型一次请求了多个动作，未执行新动作。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if not calls:
            if not isinstance(content, str) or not content.strip():
                current.status, current.response = "error", "模型返回空内容，未执行新动作。"
            else:
                current.status = "partial" if any((s.get("result") or {}).get("result") != "ok" for s in current.steps) else "ok"
                current.decision_draft = content.strip()
                current.response = ""
                emit("reply.draft", turn=turns, reply=current.decision_draft)
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        call = calls[0]
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("args"), dict):
            current.status, current.response = "error", "工具调用格式无效，未执行新动作。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        name = call["name"]
        if name not in tool_schemas:
            current.status, current.response = "rejected", "模型请求了未授权工具，未执行动作。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        declaration = tool_schemas[name]
        args = call["args"]
        if inspect.isclass(declaration) and issubclass(declaration, BaseModel):
            try:
                args = declaration.model_validate(args).model_dump(exclude_none=True)
            except ValidationError:
                current.status, current.response = "rejected", "工具参数未通过校验，未执行动作。"
                return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        if name == "ask_user":
            question = args.get("question")
            if not isinstance(question, str) or not question.strip():
                current.status, current.response = "error", "追问内容无效。"
                return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
            return {"run": asdict(current), "decision": {"kind": "ask", "question": question[:500]}, "turns": turns}
        if effectful(name) and state["effects"] >= max_effects:
            current.status, current.response = "blocked", "任务动作次数已达上限；其余步骤未执行。"
            return {"run": asdict(current), "decision": {"kind": "finish"}, "turns": turns}
        return {"run": asdict(current), "decision": {"kind": "tool", "name": name,
                "params": args, "step_id": call.get("id") or uuid.uuid4().hex}, "turns": turns}

    async def model_route(state: _State) -> str:
        return state["decision"]["kind"]

    async def tool_node(state: _State) -> dict:
        current = GraphRun(**state["run"])
        decision = state["decision"]
        name, params, step_id = decision["name"], decision["params"], decision["step_id"]
        emit("graph.node", node="tool", phase="entered", step_id=step_id, tool=name)
        is_effect = effectful(name)
        step = {"step_id": step_id, "tool_call_id": step_id, "tool": name,
                "params": params, "effectful": is_effect,
                "dispatch_status": "pending", "result": None}
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
                    result = await asyncio.wait_for(_invoke(execute, name, params, step_id),
                                                    timeout=max(0.1, current.deadline - time.time()))
                if isinstance(result, dict) and result.get("result") not in {
                    "unknown", "timeout", "executing", "accepted"
                }:
                    ledger.mark_result(current.agent_id, thread_id,
                                       current.run_id, step_id, result)
            else:
                result = await asyncio.wait_for(_invoke(execute, name, params, step_id),
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
        if not isinstance(result, dict) or result.get("result") not in {"ok", *UNCERTAIN, *FAILURE}:
            result = {"result": "error", "error": "invalid_tool_result"}
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
        elif name in terminal_tools:
            current.status = result["result"]
            current.response = result.get("message") or result.get("reason") or result.get("error") or result["result"]
        return {"run": asdict(current), "effects": state["effects"] + int(is_effect)}

    async def tool_route(state: _State) -> str:
        result = (state["run"]["steps"][-1].get("result") or {}).get("result")
        tool_name = state["run"]["steps"][-1].get("tool")
        return "finish" if tool_name in terminal_tools or result in UNCERTAIN | {"cancelled"} else "model"

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
        return {"run": asdict(current)}

    async def ask_node(state: _State) -> dict:
        question = state["decision"]["question"]
        answer = interrupt({"question": question, "run_id": state["run"]["run_id"]})
        current = GraphRun(**state["run"])
        current.question = ""
        current.supplement = str(answer)
        current.status = "executing"
        return {"run": asdict(current)}

    graph = StateGraph(_State)
    graph.add_node("model", model_node)
    graph.add_node("tool", tool_node)
    graph.add_node("ask", ask_node)
    graph.add_node("finalize", finalize_node)
    graph.add_conditional_edges(START, start_route, {"tool": "tool", "model": "model"})
    graph.add_conditional_edges("model", model_route, {"tool": "tool", "ask": "ask", "finish": "finalize"})
    graph.add_conditional_edges("tool", tool_route, {"model": "model", "finish": "finalize"})
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
            output = await compiled.ainvoke({"run": asdict(run), "decision": first,
                                             "turns": len(run.steps),
                                             "effects": sum(bool(s.get("effectful")) for s in run.steps)},
                                            config, durability="sync")
    if "__interrupt__" in output:
        payload = output["__interrupt__"][0].value
        run.status, run.question, run.response = "needs_clarification", payload["question"], payload["question"]
        emit("run.paused", reason="ask_user", question=run.question)
    else:
        updated = GraphRun(**output["run"])
        run.__dict__.update(updated.__dict__)
    if record:
        record(run, None)
    if on_progress:
        on_progress("summarizing", None)
    emit("run.finished", status=run.status, response=run.response)
    return run
