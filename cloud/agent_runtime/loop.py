"""A small agent loop. Device adapters own prompts, schemas and execution policy.

One call is executed per model turn. This makes dependencies explicit: the next
model turn sees the actual outcome before it can request another side effect.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from cloud.observability import emit_agent_event

logger = logging.getLogger(__name__)
UNCERTAIN = {"accepted", "executing", "timeout"}
FAILURE = {"blocked", "rejected", "cancelled", "error", "needs_clarification"}


@dataclass
class Run:
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


def parse_decision(response: object) -> dict:
    """Accept native ChatOpenAI calls or a strict JSON adapter, never raw prose as a call."""
    calls = getattr(response, "tool_calls", None)
    if calls:
        if len(calls) != 1:
            raise ValueError("exactly_one_call_per_turn")
        call = calls[0]
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or not isinstance(call.get("args"), dict):
            raise ValueError("invalid_native_tool_call")
        return {"tool": call["name"], "params": call["args"],
                "tool_call_id": call.get("id") or uuid.uuid4().hex}
    content = getattr(response, "content", response)
    if not isinstance(content, str):
        raise ValueError("invalid_model_content")
    content = content.strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("decision_must_be_object")
    if set(parsed) == {"final"} and isinstance(parsed["final"], str):
        return parsed
    if set(parsed) == {"tool", "params"} and isinstance(parsed["tool"], str) and isinstance(parsed["params"], dict):
        return {**parsed, "tool_call_id": uuid.uuid4().hex}
    raise ValueError("invalid_decision_shape")


async def run_loop(
    run: Run,
    decide: Callable[[Run], Awaitable[dict]],
    execute: Callable[[str, dict, str], Awaitable[dict]],
    *,
    record: Callable[[Run, dict | None], None] | None = None,
    max_turns: int = 12,
    max_effects: int = 6,
    effectful: Callable[[str], bool] = lambda _name: True,
    on_step: Callable[[dict], None] | None = None,
    on_progress: Callable[[str, dict | None], None] | None = None,
) -> Run:
    """Run one verified step at a time; uncertain outcomes stop dependent work.

    The record callback writes the dispatch intent *before* execute. On resume,
    callers must reconcile any pre-dispatch/unknown step by its stable ID.
    """
    def emit(event: str, **fields: object) -> None:
        emit_agent_event(event, trace_id=run.trace_id or run.run_id,
                         run_id=run.run_id, parent_run_id=run.parent_run_id or None,
                         agent_id=run.agent_id or None, session_id=run.session_id,
                         origin_kind=run.origin_kind or None, **fields)

    emit("run.started", goal=run.goal, resumed=bool(run.steps))
    if on_progress:
        on_progress("planning", None)
    effects = sum(bool(step.get("effectful")) for step in run.steps)
    execution_announced = False
    for turn in range(max_turns):
        if time.time() >= run.deadline:
            run.status, run.response = "timeout", "任务已到期；未确认的动作不会自动重发。"
            break
        if on_progress and run.steps:
            on_progress("considering", run.steps[-1])
        model_started = time.monotonic()
        try:
            decision = await asyncio.wait_for(decide(run),
                                              timeout=max(0.1, run.deadline - time.time()))
        except Exception as exc:
            error_id = uuid.uuid4().hex
            elapsed_ms = round((time.monotonic() - model_started) * 1000)
            cause = exc.__cause__ or exc.__context__
            cause_type = type(cause).__name__ if cause is not None else "-"
            logger.warning("agent session=%s agent=%s run=%s model turn=%d failed error=%s cause=%s elapsed_ms=%d error_id=%s",
                           run.session_id, run.agent_id, run.run_id, turn + 1,
                           type(exc).__name__, cause_type, elapsed_ms, error_id)
            emit("model.error", turn=turn + 1, stage="decide", error=type(exc).__name__,
                 cause=cause_type if cause is not None else None,
                 message=str(exc), stack=traceback.format_tb(exc.__traceback__), error_id=error_id,
                 duration_ms=elapsed_ms)
            run.status, run.response = "error", "暂时无法可靠理解下一步；已执行的动作不会重发。"
            break
        emit("model.decision", turn=turn + 1, decision=decision,
             decision_source=decision.get("decision_source", "model") if isinstance(decision, dict) else "model",
             duration_ms=round((time.monotonic() - model_started) * 1000))
        if not isinstance(decision, dict):
            run.status, run.response = "error", "模型返回格式无效，未执行新动作。"
            break
        if "final" in decision:
            emit("reply.draft", turn=turn + 1, reply=decision["final"], source="model",
                 reply_kind="final")
            run.status = ("partial" if any((step.get("result") or {}).get("result") != "ok"
                                           for step in run.steps) else "ok")
            # A final claim cannot establish completion after a failed step.
            # The device adapter asks its persona to explain the recorded facts.
            run.decision_draft = str(decision["final"])
            run.response = ""
            run.reply_source = "runtime"
            emit("reply.validated", turn=turn + 1, reply=run.response,
                 changed=run.response != str(decision["final"]),
                 reason="execution_facts" if run.response != str(decision["final"]) else "unchanged",
                 reply_kind="final")
            break
        name, params = decision.get("tool"), decision.get("params")
        if not isinstance(name, str) or not isinstance(params, dict):
            run.status, run.response = "error", "工具调用格式无效，未执行新动作。"
            break
        if name == "ask_user":
            question = params.get("question")
            if not isinstance(question, str) or not question.strip():
                run.status, run.response = "error", "追问内容无效。"
            else:
                run.status, run.question, run.response = "needs_clarification", question[:500], question[:500]
                run.reply_source = "model"
            emit("run.paused" if run.status == "needs_clarification" else "model.error",
                 turn=turn + 1, reason="ask_user", question=run.question or None)
            break
        is_effect = effectful(name)
        if is_effect and effects >= max_effects:
            run.status, run.response = "blocked", "任务动作次数已达上限；其余步骤未执行。"
            break
        step_id = uuid.uuid4().hex
        step = {"step_id": step_id, "tool_call_id": decision.get("tool_call_id") or step_id,
                "tool": name, "params": params, "fingerprint": hashlib.sha256(
                    json.dumps([name, params], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "effectful": is_effect, "dispatch_status": "pending", "result": None}
        if on_progress and not execution_announced:
            on_progress("executing", None)
            execution_announced = True
        run.steps.append(step)
        if record:
            record(run, step)
        if on_progress:
            on_progress("decision", step)
            on_progress("dispatching", step)
        emit("tool.dispatch", turn=turn + 1, step_id=step_id,
             tool_call_id=step["tool_call_id"], tool=name, params=params,
             dispatch_status="pending", effectful=is_effect)
        tool_started = time.monotonic()
        tool_error_stack = None
        try:
            result = await asyncio.wait_for(execute(name, params, step_id),
                                            timeout=max(0.1, run.deadline - time.time()))
        except asyncio.TimeoutError:
            result = {"result": "timeout", "error": "run_deadline_exceeded"}
        except asyncio.CancelledError:
            step.update(dispatch_status="unknown", result={"result": "cancelled", "error": "execution_cancelled"})
            if record:
                record(run, step)
            emit("tool.result", turn=turn + 1, step_id=step_id, tool=name,
                 status="unknown", result=step["result"],
                 duration_ms=round((time.monotonic() - tool_started) * 1000))
            emit("run.cancelled", reason="execution_cancelled")
            raise
        except Exception as exc:
            tool_error_stack = traceback.format_tb(exc.__traceback__)
            logger.warning("agent run=%s step=%s tool=%s failed type=%s",
                           run.run_id, step_id, name, type(exc).__name__)
            result = {"result": "error", "error": type(exc).__name__}
        if not isinstance(result, dict) or result.get("result") not in {"ok", *UNCERTAIN, *FAILURE}:
            result = {"result": "error", "error": "invalid_tool_result"}
        step.update(dispatch_status="observed", result=result)
        if is_effect:
            effects += 1
        if record:
            record(run, step)
        emit("tool.result", turn=turn + 1, step_id=step_id, tool=name,
             status=result["result"], result=result,
             request_id=result.get("request_id"), task_id=result.get("task_id"),
             stack=tool_error_stack,
             duration_ms=round((time.monotonic() - tool_started) * 1000))
        if on_step:
            on_step(step)
        if on_progress:
            on_progress("observed", step)
        logger.info("agent run=%s step=%s tool=%s result=%s", run.run_id, step_id, name, result["result"])
        if result["result"] in UNCERTAIN:
            run.status = result["result"]
            run.response = "请求已发送，但实际完成情况尚未确认；后续步骤已暂停。"
            emit("run.paused", turn=turn + 1, reason=result["result"], step_id=step_id)
            break
        if result["result"] == "cancelled":
            run.status = "cancelled"
            run.response = "任务已取消；后续步骤未执行。"
            break
        # A failure is observable to the model. It may choose a legal alternative
        # or explain partial completion on the next turn.
    else:
        run.status, run.response = "blocked", "模型轮次已达上限；其余步骤未执行。"
    if record:
        record(run, None)
    if on_progress:
        on_progress("summarizing", None)
    if run.status in {"error", "blocked", "timeout", "unknown"}:
        emit("reply.fallback", status=run.status, reply=run.response,
             reason="runtime_terminal_state", reply_kind="final")
    confirmed = [s["step_id"] for s in run.steps if (s.get("result") or {}).get("result") == "ok"]
    unconfirmed = [s["step_id"] for s in run.steps if (s.get("result") or {}).get("result") in UNCERTAIN
                   or s.get("dispatch_status") != "observed"]
    failed = [s["step_id"] for s in run.steps if (s.get("result") or {}).get("result") in FAILURE]
    emit("run.finished", status=run.status, response=run.response,
         confirmed_steps=confirmed, unconfirmed_steps=unconfirmed, failed_steps=failed)
    return run
