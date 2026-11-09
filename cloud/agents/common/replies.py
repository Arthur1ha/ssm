"""Facts for a persona reply after the tool loop stops without a safe final."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from cloud.agents.common.runner import GraphRun as Run
from cloud.agents.common.messages import reply_messages
from cloud.observability import emit_agent_event

logger = logging.getLogger(__name__)


def step_kind(step: dict) -> str:
    """Persistence and delivery are effects, but are not physical completion."""
    result = step.get("result") or {}
    if result.get("result_kind"):
        return result["result_kind"]
    tool = step.get("tool")
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
    try:
        if run.decision_draft:
            answer, source = run.decision_draft, "decision_model"
        elif run.steps and time.time() < run.deadline:
            prompt = instructions(run) if callable(instructions) else instructions
            messages = reply_messages(prompt + "\n本步骤仅输出给用户的自然语言回复。", run,
                                      context=context)
            emit_agent_event("model.request", **fields, input=messages, goal=run.goal,
                             template_version="factual-reply-v2")
            client = model if hasattr(model, "ainvoke") else model()
            response = await asyncio.wait_for(client.ainvoke(messages),
                                              timeout=max(0.1, run.deadline - time.time()))
            answer, source = getattr(response, "content", ""), "persona_model"
            emit_agent_event("model.response", **fields, output=answer)
        else:
            run.response, run.reply_source = fallback(run), "runtime"
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
    return run


def valid_reply_format(value: object, run: Run | None = None) -> bool:
    """Check the text envelope only; wording cannot prove device completion."""
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 4000
            and not value.lstrip().startswith(("{", "[", "```")))


def factual_fallback(agent_id: str, status: str, run: Run | None = None) -> str:
    """Report observed results when a role's reply model is unavailable."""
    role = "go2" if agent_id == "go2" else ("lamp" if agent_id.startswith("esp32") else "orchestrator")
    if status in {"accepted", "executing", "timeout", "unknown"}:
        return {"go2": "我还没收到动作完成的确认。", "lamp": "我还没收到调灯完成的确认。",
                "orchestrator": "这项安排尚无完成确认。"}[role]
    if status == "error" and run is not None and not run.steps:
        return {"go2": "我这次没能好好处理你的话，稍后再问我一下。",
                "lamp": "我这次处理请求出了问题，还没做任何动作。稍后再试。",
                "orchestrator": "这次对话处理出了问题，请稍后再试。"}[role]
    if run is not None and run.steps:
        results = [(step, step.get("result") or {}) for step in run.steps]
        completed = [step for step, result in results
                     if step_kind(step) == "action" and result.get("result") == "ok"]
        proposals = [step for step, result in results
                     if step_kind(step) == "training_proposal" and result.get("result") == "ok"]
        failed = [result for _, result in results if result.get("result") in
                  {"blocked", "rejected", "error", "timeout", "unknown", "cancelled"}]
        offline = any(result.get("device_connected") is False
                      or result.get("device_fsm") == "offline"
                      or result.get("error") in {"device_precondition", "robot_disconnected", "offline"}
                      or any((item.get("result") or {}).get("device_connected") is False
                             or (item.get("result") or {}).get("device_fsm") == "offline"
                             for item in (result.get("tool_results") or [])
                             if isinstance(item, dict))
                      for _, result in results)
        if role == "go2" and (offline or status == "offline"):
            return ("汪，你的话我听到了，但机身还没连上。"
                    + ("前面的步骤有结果了，后面的动作这次做不了。" if completed
                       else "这次动作没能执行。"))
        if failed:
            reason = failed[-1].get("message") or failed[-1].get("error") or ""
            reason = reason if isinstance(reason, str) and reason and len(reason) <= 80 else ""
            if completed:
                prefix = {"go2": "汪，前面的动作完成了，后面的没做成。",
                          "lamp": "前一步已经到位，后一步没做成。",
                          "orchestrator": "部分安排已完成，其余未完成。"}[role]
            elif not any(step_kind(step) == "action" for step, _ in results):
                prefix = "这次要求还没有处理成功。"
            else:
                prefix = {"go2": "汪，这次动作还没做成。",
                          "lamp": "这次没调成。",
                          "orchestrator": "这次安排没能完成。"}[role]
            return prefix + (f"反馈：{reason}。" if reason else "")
        if role == "lamp" and completed and status == "error":
            return "已执行的灯光调整已生效，但这次对话处理出了问题。"
        if proposals and not completed:
            return "这项要求已整理好，请核对对话中的修改卡片。"
        if not completed and status == "ok":
            kinds = {step_kind(step) for step, result in results if result.get("result") == "ok"}
            if "message" in kinds:
                return "消息已发出，等待对方回应。"
            if "configuration" in kinds:
                return "这项要求已记录。"
            if "activity" in kinds:
                return "活动已启动，设备的加入情况已更新。"
            return "这次查询已返回结果。"
    if status == "offline":
        return {"go2": "呜，我的身体还未连接，这次动作做不了。",
                "lamp": "设备当前离线，这次没能调整灯光。",
                "orchestrator": "设备当前离线，这次安排无法完成。"}[role]
    if status == "ok":
        if run is not None and not any(
            step_kind(step) == "action" and (step.get("result") or {}).get("result") == "ok"
            for step in run.steps
        ):
            return {"go2": "唔，我这会儿没法好好回答。等一下再问我，好吗？",
                    "lamp": "我这会儿没法给出可靠回答，稍后再试。",
                    "orchestrator": "当前无法可靠回答，请稍后再试。"}[role]
        return {"go2": "好耶，这件事我办好了！", "lamp": "这才像样，灯已经调好了。",
                "orchestrator": "这次安排已完成。"}[role]
    if status == "partial":
        return {"go2": "有一部分做好啦，剩下的还没完成呢。", "lamp": "部分调整已完成，其余还没有完成。",
                "orchestrator": "部分安排已经完成，其余还没完成。"}[role]
    if status == "needs_clarification":
        return {"go2": "我还没听明白要做什么呀，能再说一点吗？", "lamp": "还需要补充具体的照明目标。",
                "orchestrator": "我还需要一个具体目标，才能继续安排。"}[role]
    if status == "cancelled":
        return {"go2": "我停下啦，这次动作取消了。", "lamp": "这次灯光调整已取消。",
                "orchestrator": "这次安排已取消。"}[role]
    return {"go2": "唔，这个动作我现在做不到。", "lamp": "这次灯光调整未完成。",
            "orchestrator": "这次安排没能完成。"}[role]


def reply_facts(run: Run) -> dict:
    """Give each role the same observations without prescribing its voice."""
    return {"goal": run.goal, "status": run.status,
             "steps": [{"tool": step.get("tool"), "params": step.get("params"),
                        "kind": step_kind(step), "effectful": step.get("effectful"),
                        "result": step.get("result") or {"result": "unknown"}}
                       for step in run.steps],
             "decision_draft": run.decision_draft, "question": run.question,
             "runtime_note": run.response}
