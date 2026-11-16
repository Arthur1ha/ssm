"""Facts for a persona reply after the tool loop stops without a safe final."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from cloud.agents.common.runner import GraphRun as Run
from cloud.agents.common.emotions import REPLY_INSTRUCTIONS, normalize_emotion, parse_emotional_reply
from cloud.agents.common.messages import reply_messages
from cloud.agents.common.outcomes import effective_steps, outcome_summary
from cloud.agents.common.tool_results import SUCCESS, UNCERTAIN
from cloud.observability import emit_agent_event

logger = logging.getLogger(__name__)


def step_kind(step: dict) -> str:
    """Persistence and delivery are effects, but are not physical completion."""
    result = step.get("result") or {}
    if result.get("result_kind"):
        return result["result_kind"]
    tool = step.get("tool")
    if tool == "apply_training":
        return "training"
    if tool == "propose_training":
        return "training_proposal"
    if tool == "list_training" or step.get("effectful") is False:
        return "query"
    if tool == "send_message":
        return "message"
    if tool == "start_activity":
        return "activity"
    if tool in {"set_space_constraint", "clear_space_constraints"}:
        return "configuration"
    return "action"


async def finalize_reply(run: Run, *, model, instructions,
                         validate: Callable | None = None, fallback: Callable | None = None,
                         context: dict | None = None) -> Run:
    """Reuse the decision reply; compose once for a direct tool ending."""
    validator = validate or valid_reply_format
    fallback = fallback or (lambda current: factual_fallback(current.agent_id, current.status, current))
    fields = dict(agent_id=run.agent_id, run_id=run.run_id,
                  trace_id=run.trace_id or run.run_id, session_id=run.session_id)
    expression_emotion = normalize_emotion(run.emotion) if run.emotion_step_id else None
    try:
        if run.decision_draft:
            answer, source = run.decision_draft, "decision_model"
            run.emotion = normalize_emotion(run.emotion)
        elif run.steps and time.time() < run.deadline:
            prompt = instructions(run) if callable(instructions) else instructions
            messages = reply_messages(prompt + "\n" + REPLY_INSTRUCTIONS, run,
                                      context=context)
            emit_agent_event("model.request", **fields, input=messages, goal=run.goal,
                             template_version="factual-reply-v2")
            client = model if hasattr(model, "ainvoke") else model()
            response = await asyncio.wait_for(client.ainvoke(messages),
                                              timeout=max(0.1, run.deadline - time.time()))
            answer, source = getattr(response, "content", ""), "persona_model"
            emit_agent_event("model.response", **fields, output=answer)
            answer, run.emotion = parse_emotional_reply(answer)
        else:
            run.response, run.reply_source = fallback(run), "runtime"
            run.emotion = "neutral"
            return run
        if not validator(answer, run):
            raise ValueError("invalid_reply_format")
        run.response, run.reply_source = answer.strip(), source
        emit_agent_event("reply.validated", **fields, reply=run.response, validation="format",
                         reason="decision_final" if source == "decision_model" else "factual_reply",
                         reply_kind="final")
    except Exception as exc:
        logger.warning("agent reply unavailable agent=%s run=%s error=%s",
                       run.agent_id, run.run_id, type(exc).__name__)
        run.response, run.reply_source = fallback(run), "runtime"
        run.emotion = "neutral"
    finally:
        if expression_emotion is not None:
            # Execution can fail while the original expressive intent remains;
            # the receipt controls factual wording, not this presentation field.
            run.emotion = expression_emotion
    return run


def valid_reply_format(value: object, run: Run | None = None) -> bool:
    """Check the text envelope only; wording cannot prove device completion."""
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 4000
            and not value.lstrip().startswith(("{", "[", "```")))


def factual_fallback(agent_id: str, status: str, run: Run | None = None) -> str:
    """Report observed results when a role's reply model is unavailable."""
    if status in UNCERTAIN:
        return "请求已提交，实际完成尚未确认。"
    if run is None or not run.steps:
        return "对话回复暂时不可用，请稍后再试。"
    results = [(step, step.get("result") or {}) for step in effective_steps(run.steps)]
    failed = [result for _, result in results if result.get("result") not in SUCCESS]
    completed = [step for step, result in results
                 if step_kind(step) == "action" and result.get("result") in SUCCESS]
    if failed:
        reason = failed[-1].get("message") or failed[-1].get("error") or failed[-1].get("reason")
        prefix = "部分操作已有成功回执，其余未完成。" if completed else "本次请求未完成。"
        return prefix + (f"执行反馈：{reason[:160]}" if isinstance(reason, str) and reason else "")
    kinds = {step_kind(step) for step, result in results if result.get("result") in SUCCESS}
    if "training_proposal" in kinds:
        return "教导草案已生成，请核对后确认；尚未执行物理动作。"
    if "activity" in kinds:
        return "活动状态已更新。"
    if "message" in kinds:
        return "消息已提交。"
    if "configuration" in kinds:
        return "配置已保存。"
    if completed:
        return "操作已有成功回执；角色回复暂时不可用。"
    return "查询已有回执；角色回复暂时不可用。"


def reply_facts(run: Run) -> dict:
    """Give each role the same observations without prescribing its voice."""
    return {"goal": run.goal, "status": run.status,
             "outcomes": outcome_summary(run.steps),
             **({"initial_context": run.initial_context} if getattr(run, "initial_context", {}) else {}),
             "steps": [{"tool": step.get("tool"), "params": step.get("params"),
                        "kind": step_kind(step), "effectful": step.get("effectful"),
                        "result": step.get("result") or {"result": "unknown"}}
                       for step in run.steps],
             "decision_draft": run.decision_draft, "question": run.question,
             "runtime_note": run.response}
