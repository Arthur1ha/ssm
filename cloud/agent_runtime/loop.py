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
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

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
) -> Run:
    """Run one verified step at a time; uncertain outcomes stop dependent work.

    The record callback writes the dispatch intent *before* execute. On resume,
    callers must reconcile any pre-dispatch/unknown step by its stable ID.
    """
    effects = sum(bool(step.get("effectful")) for step in run.steps)
    for turn in range(max_turns):
        if time.time() >= run.deadline:
            run.status, run.response = "timeout", "任务已到期；未确认的动作不会自动重发。"
            break
        try:
            decision = await decide(run)
        except Exception as exc:
            logger.warning("agent run=%s model turn=%d failed: %s", run.run_id, turn, type(exc).__name__)
            run.status, run.response = "error", "暂时无法可靠理解下一步；已执行的动作不会重发。"
            break
        if not isinstance(decision, dict):
            run.status, run.response = "error", "模型返回格式无效，未执行新动作。"
            break
        if "final" in decision:
            completed = [f"第{i}步" for i, s in enumerate(run.steps, 1)
                         if s.get("result", {}).get("result") == "ok"]
            incomplete = [f"第{i}步 ({(s.get('result') or {}).get('result', 'unknown')})"
                          for i, s in enumerate(run.steps, 1)
                          if s.get("result", {}).get("result") != "ok"]
            run.status = "partial" if incomplete else "ok"
            if incomplete:
                run.response = ("已确认：" + ("、".join(completed) if completed else "无")
                                + "；未完成或未确认：" + "、".join(incomplete) + "。")
            elif any(s.get("effectful") for s in run.steps):
                run.response = "已确认完成：" + "、".join(completed) + "。"
            else:
                run.response = str(decision["final"])
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
        run.steps.append(step)
        if record:
            record(run, step)
        try:
            result = await asyncio.wait_for(execute(name, params, step_id),
                                            timeout=max(0.1, run.deadline - time.time()))
        except asyncio.TimeoutError:
            result = {"result": "timeout", "error": "run_deadline_exceeded"}
        except asyncio.CancelledError:
            step.update(dispatch_status="unknown", result={"result": "cancelled", "error": "execution_cancelled"})
            if record:
                record(run, step)
            raise
        except Exception as exc:
            logger.exception("agent run=%s step=%s tool=%s failed", run.run_id, step_id, name)
            result = {"result": "error", "error": type(exc).__name__}
        if not isinstance(result, dict) or result.get("result") not in {"ok", *UNCERTAIN, *FAILURE}:
            result = {"result": "error", "error": "invalid_tool_result"}
        step.update(dispatch_status="observed", result=result)
        if is_effect:
            effects += 1
        if record:
            record(run, step)
        if on_step:
            on_step(step)
        logger.info("agent run=%s step=%s tool=%s result=%s", run.run_id, step_id, name, result["result"])
        if result["result"] in UNCERTAIN:
            run.status = result["result"]
            run.response = "请求已发送，但实际完成情况尚未确认；后续步骤已暂停。"
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
    return run
