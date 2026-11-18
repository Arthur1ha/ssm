"""One device voice per coordinated user turn, based on all observed steps."""

from __future__ import annotations

import time
import uuid
import os
from copy import deepcopy

from cloud.agents.common.runner import GraphRun
from cloud.agents.common.replies import finalize_reply
from cloud.agents.common.tool_results import SUCCESS
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.emotions import normalize_emotion
from cloud.observability import emit_agent_event


def device_outcomes(run: GraphRun) -> dict[str, dict]:
    """Project receipts without turning a successful query into action success."""
    groups = {}
    updates = {step["result"]["task_id"]: step["result"] for step in run.steps
               if step.get("tool") == "await_result"
               and isinstance(step.get("result"), dict) and step["result"].get("task_id")}
    for step in run.steps:
        result = step.get("result") or {}
        result = {**result, **updates.get(result.get("task_id"), {})}
        params = step.get("params") or {}
        if step.get("tool") not in {"invoke_skill", "apply_training"}:
            continue
        unit_id = result.get("unit_id") or params.get("unit_id")
        if unit_id not in {"go2", os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")}:
            continue
        group = groups.setdefault(unit_id, {"steps": [], "physical_state": {}, "calls": 0,
                                            "decision_reply": "", "emotion": "neutral"})
        group["calls"] += 1
        if result.get("reply_source") != "runtime":
            group["decision_reply"] = result.get("response") or result.get("reply") or ""
            group["emotion"] = normalize_emotion(result.get("emotion"))
        nested = result.get("tool_results") or result.get("steps")
        if isinstance(nested, list) and nested:
            for item in nested:
                receipt = item.get("result") or {}
                group["steps"].append({
                    "step_id": item.get("step_id") or step["step_id"],
                    "tool": item.get("tool") or result.get("skill_id"),
                    "params": item.get("params") or {}, "result": deepcopy(receipt),
                    "effectful": item.get("effectful", item.get("tool") != "go2_status"),
                })
                if receipt.get("physical_state"):
                    group["physical_state"] = deepcopy(receipt["physical_state"])
        else:
            receipt = {key: deepcopy(value) for key, value in result.items()
                       if key not in {"response", "reply", "agent_reply", "agent_reply_event_id"}}
            group["steps"].append({
                "step_id": step["step_id"],
                "tool": result.get("skill_id") or params.get("skill_id") or step["tool"],
                "params": params.get("params") or params,
                "result": receipt, "effectful": params.get("skill_id") != "go2_status",
            })
        if result.get("physical_state"):
            group["physical_state"] = deepcopy(result["physical_state"])
    for group in groups.values():
        actions = [step for step in group["steps"] if step.get("effectful") is not False]
        group["has_action"] = bool(actions)
        results = [(step.get("result") or {}).get("result", "unknown")
                   for step in actions or group["steps"]]
        failures = [value for value in results if value not in SUCCESS]
        group["status"] = ("ok" if not failures else "partial" if any(value in SUCCESS for value in results)
                           else failures[-1])
    return groups


async def publish_device_replies(run: GraphRun, *, context: dict, transport, model_factory) -> None:
    """Use the existing role reply composer; never dispatch another action."""
    outcomes = device_outcomes(run)
    run.initial_context["delegated_device_outcomes"] = outcomes
    if (context.get("origin_kind", "user_delegated") != "user_delegated"
            or context.get("peer_request_id") or run.status == "needs_clarification"):
        return
    for unit_id, outcome in outcomes.items():
        if not outcome["has_action"]:
            continue
        if time.time() >= min(run.deadline, float(context.get("expires_at") or run.deadline)):
            break
        role = "go2" if unit_id == "go2" else "lamp"
        if role == "go2":
            from cloud.agents.go2.prompts import persona_reply_prompt
        else:
            from cloud.agents.lamp.prompts import persona_reply_prompt
        reply_id = "reply_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{run.run_id}:{unit_id}").hex
        device_run = GraphRun(
            reply_id, run.session_id, run.goal, min(run.deadline, time.time() + 12),
            steps=outcome["steps"], status=outcome["status"], agent_id=unit_id,
            trace_id=run.trace_id, parent_run_id=run.run_id, origin_kind="user_delegated",
            goal_revision=run.goal_revision,
            initial_context={"physical_state": outcome["physical_state"]},
            decision_draft=outcome["decision_reply"] if outcome["calls"] == 1 else "",
            emotion=outcome["emotion"],
        )
        ledger = FileEffectLedger()
        identity = (unit_id, "device-replies:" + run.session_id, run.run_id, reply_id)
        claimed = ledger.reserve(*identity, "compose_reply", {"unit_id": unit_id})
        if claimed["reservation"] == "existing":
            saved = claimed.get("result")
            if not saved:
                # A crashed/failed generation is not repeated on event replay.
                continue
            device_run.response = saved.get("text", "")
            device_run.emotion = saved.get("emotion", "neutral")
            device_run.reply_source = saved.get("reply_source", "runtime")
        else:
            await finalize_reply(
                device_run, model=lambda: model_factory(role),
                instructions=lambda value: persona_reply_prompt(value) +
                    "\n只回应这轮请求中属于自己的目标和最终真实结果，通常一句即可。"
                    "中间步骤和内部状态查询合并表达；最后物理状态优先于先前目标参数。"
                    "动作已观测开始但未确认结束时，只说完成尚未确认，不说从未执行。"
                    "不要替同伴总结，不追加追问、建议或自动补做的承诺。",
            )
            ledger.mark_result(*identity, {"text": device_run.response,
                "emotion": device_run.emotion, "reply_source": device_run.reply_source})
        publication = (*identity[:3], reply_id + ":publish")
        sent = ledger.reserve(*publication, "publish_reply", {"event_id": reply_id})
        if sent["reservation"] == "existing":
            continue
        transport.do_publish_ui_event(
            run.session_id, "agent_reply" if device_run.reply_source != "runtime" else "reply_error", run_id=reply_id,
            trace_id=run.trace_id or run.run_id, status=outcome["status"],
            text=device_run.response, emotion=device_run.emotion, reply_source=device_run.reply_source,
            event_id=reply_id, agent_id=unit_id, parent_run_id=run.run_id,
            source="delegated_skill", step_id=unit_id, reply_kind="final",
            goal_revision=run.goal_revision,
        )
        ledger.mark_result(*publication, {"event_id": reply_id})
        emit_agent_event("reply.emitted", agent_id=unit_id, run_id=reply_id,
                         parent_run_id=run.run_id, session_id=run.session_id,
                         trace_id=run.trace_id or run.run_id, reply_kind="final",
                         channel="mqtt_feedback", reply=device_run.response)
