"""Card-backed steward tools and trusted domain execution."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from pydantic import BaseModel, Field, ConfigDict
from cloud.agents.common.runner import invoke_blocking
from cloud.observability import current_event_context, emit_agent_event
from cloud.agents.steward.tools.coordination import allows_step
from cloud.agents.steward.tools.card import validate_public_skill
from cloud.peers.cancellation import peer_coordination_cancelled
from cloud.agents.steward.tools.policy import http_timeout_for, skill_requires_persistence
from cloud.space.registry import DEFAULT_SPACE_ID, get_space_cards, get_space_registry
from cloud.space.context import get_space_context
from cloud.space.constraints import get_constraint_store
from cloud.orchestrator import tools as transport

logger = logging.getLogger("agents.steward.tools")

def _cards(context: dict) -> dict:
    cards = transport._registry.get_all_cards() if transport._registry else {}
    if not isinstance(cards, dict):
        return {}
    space_id = context.get("space_id") or DEFAULT_SPACE_ID
    if space_id != DEFAULT_SPACE_ID:
        return {}
    # Final membership check is repeated by validate_public_skill at dispatch.
    return {uid: card for uid, card in cards.items() if isinstance(card, dict)}

def _public_view(cards: dict) -> list:
    return [{"unit_id": uid, "online": card.get("online", False),
             "name": card.get("name"), "services": card.get("services", {}),
             "observations": card.get("observations", []),
             "skills": [{"id": skill.get("id"), "name": skill.get("name"),
                         "description": skill.get("description"),
                         "params_schema": skill.get("params_schema", {})}
                        for skill in card.get("skills", [])]}
            for uid, card in cards.items()]

def _wait_mqtt_result(task_id: str, session_id: str, deadline: float,
                      *, unit_id: str | None = None) -> dict:
    while time.time() < deadline:
        result = None
        if unit_id is not None:
            from cloud.esp32.physical_tasks import get as get_physical_result
            result = get_physical_result(task_id, unit_id=unit_id, session_id=session_id)
        if not result or result.get("result") in {"accepted", "executing", "unknown", "timeout"}:
            result = transport._state.get_task_result(task_id, session_id) if transport._state else result
        if isinstance(result, dict):
            status = result.get("result")
            if status in {"ok", "blocked", "rejected", "error", "cancelled", "timeout"}:
                return result
        time.sleep(0.1)
    return {"result": "timeout", "task_id": task_id, "error": "edge_result_unconfirmed"}

def _invoke_skill(context: dict, arguments: dict, step_id: str, deadline: float) -> dict:
    unit_id, skill_id, params = (arguments.get("unit_id"), arguments.get("skill_id"),
                                 arguments.get("params"))
    if not isinstance(unit_id, str) or not isinstance(skill_id, str) or not isinstance(params, dict):
        return {"result": "rejected", "error": "invalid_skill_arguments"}
    card = _cards(context).get(unit_id)
    skill = next((s for s in card.get("skills", []) if s.get("id") == skill_id), None) if card else None
    if not skill:
        return {"result": "blocked", "error": "target_or_skill_unavailable"}
    if skill_requires_persistence(skill) and not context.get("persist"):
        return {"result": "rejected", "error": "persistence_permission_denied"}
    task = {"unit_id": unit_id, "skill_id": skill_id, "params": params, "task_id": step_id}
    try:
        selected = validate_public_skill(card, skill, params, context)
        kind = selected.get("kind")
        if context.get("origin_kind") and not allows_step(task, skill, kind, context["origin_kind"]):
            return {"result": "blocked", "error": "coordination_skill_denied"}
    except (ValueError, PermissionError) as exc:
        return {"result": "rejected", "error": str(exc)[:120]}
    if kind == "mqtt":
        from cloud.esp32.tool_executor import execute_physical
        from cloud.esp32.tool_registry import PHYSICAL_ACTIONS
        from cloud.esp32.physical_tasks import record_result
        from cloud.space.execution import ExecutionContext
        execution = ExecutionContext(
            context["session_id"], context.get("actor", "visitor"), bool(context.get("persist")),
            context.get("space_id", "main"), context.get("origin_kind", "user_delegated"),
            context.get("expires_at") or 0)

        def dispatch(uid, action, physical_params, trusted, source, *, task_id):
            transport.do_publish_task(uid, task_id, action, physical_params,
                                      trusted.session_id, context={
                                          "actor": trusted.actor, "persist": trusted.persist,
                                          "space_id": trusted.space_id, "source": source})
            return task_id

        result = execute_physical(
            state=transport._state, unit_id=unit_id,
            action=(skill.get("invoke") or {}).get("action", ""), params=params,
            context=execution, dispatch=dispatch, task_id=step_id,
            card=card, source="orchestrator", wait=False)
        if result.get("result") in {"accepted", "executing", "unknown"}:
            action = (skill.get("invoke") or {}).get("action", "")
            result = _wait_mqtt_result(step_id, context["session_id"],
                                       min(deadline, time.time() + PHYSICAL_ACTIONS[action]["timeout"]),
                                       unit_id=unit_id)
            if result.get("result") != "timeout":
                record_result(step_id, unit_id, result)
        emit_agent_event("device.result", task_id=step_id, target_unit_id=unit_id,
                         status=result.get("result"), result=result,
                         completion_basis="edge_result" if result.get("result") != "timeout" else "unconfirmed")
        if (unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")
                and context.get("origin_kind", "user_delegated") == "user_delegated"
                and result.get("result") in {"ok", "accepted", "executing", "timeout", "unknown",
                                             "rejected", "blocked", "cancelled", "error"}):
            correlation = current_event_context()
            try:
                voice = transport.do_http_dispatch(
                    f"/api/internal/esp32/{unit_id}/agent/replies",
                    {"session_id": context["session_id"], "actor": context.get("actor"),
                     "persist": context.get("persist"), "space_id": context.get("space_id"),
                     "origin_kind": "user_delegated", "expires_at": context.get("expires_at"),
                     "task_id": step_id, "goal": arguments.get("message") or
                     f"{skill_id}: {json.dumps(params, ensure_ascii=False)}",
                     "action": (skill.get("invoke") or {}).get("action", ""),
                     "params": params, "trace_id": correlation.get("trace_id") or "",
                     "parent_run_id": correlation.get("run_id") or ""},
                    min(12, max(2, deadline - time.time())))
                if voice.get("response"):
                    result["agent_reply"] = voice["response"]
                    result["agent_reply_event_id"] = voice.get("event_id")
            except Exception as exc:
                logger.warning("lamp delegated reply unavailable step=%s error=%s",
                               step_id, type(exc).__name__)
    elif kind == "http":
        from cloud.agents.go2.workflows.search import SEARCH_SERVER_TIMEOUT_S
        correlation = current_event_context()
        body = {"session_id": context["session_id"], "actor": context.get("actor"),
                "persist": context.get("persist"), "space_id": context.get("space_id"),
                "origin_kind": context.get("origin_kind") or "user_delegated",
                "expires_at": context.get("expires_at"),
                "message": arguments.get("message") or params.get("message") or params.get("goal")
                           or f"请执行 {skill_id}：{json.dumps(params, ensure_ascii=False)}",
                "goal": params.get("goal"), "skill_id": skill_id, "params": params,
                "task_id": step_id,
                "deadline": min(deadline, time.time() + (SEARCH_SERVER_TIMEOUT_S if skill_id == "go2_find_object"
                                                        else 160 if skill_id == "go2_visual_navigate" else 90)),
                "trace_id": correlation.get("trace_id"),
                "parent_run_id": correlation.get("run_id")}
        timeout = (max(1, deadline - time.time()) if unit_id == "go2" or skill_id == "adjust_lighting"
                   else min(http_timeout_for(card, skill_id), max(1, deadline - time.time())))
        try:
            result = transport.do_http_dispatch(selected.get("endpoint", ""), body, timeout)
        except Exception as exc:
            logger.warning("run step=%s http failed: %s", step_id, type(exc).__name__)
            result = {"result": "timeout", "error": "http_result_unknown", "task_id": step_id}
    else:
        result = {"result": "rejected", "error": "unsupported_transport"}
    return {**result, "task_id": result.get("task_id") or step_id,
            "query_endpoint": selected.get("endpoint") if kind == "http" else None,
            "unit_id": unit_id, "skill_id": skill_id}

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ListAgents(_Args):
    """读取当前空间设备卡与公开技能。"""

class GetSpaceContext(_Args):
    """读取可信空间现状。"""

class ReadState(_Args):
    """读取传感器与执行器快照。"""

class InvokeSkill(_Args):
    """按当前 Card 调用一个公开设备技能并等待实体结果。"""
    unit_id: str
    skill_id: str
    params: dict[str, Any]
    message: str | None = None

class AwaitResult(_Args):
    """查询既有设备任务结果。"""
    task_id: str

class SendMessage(_Args):
    """向同空间智能体发送受限消息。"""
    target: str
    content: str
    reply_to: str | None = None

class StartActivity(_Args):
    """根据已验证的直接用户请求启动空间活动。"""

class SetSpaceConstraint(_Args):
    """分享用户明确提出的当前空间要求。"""
    text: str
    targets: list[str]
    ttl_s: int = Field(default=300, ge=1, le=3600)

class ClearSpaceConstraints(_Args):
    """撤销本会话空间要求。"""

class AskUser(_Args):
    """暂停执行，向用户追问必要信息。"""
    question: str

STEWARD_TOOL_SCHEMAS = {
    "list_agents": ListAgents, "get_space_context": GetSpaceContext,
    "read_state": ReadState, "invoke_skill": InvokeSkill,
    "await_result": AwaitResult,
    "send_message": SendMessage, "start_activity": StartActivity,
    "set_space_constraint": SetSpaceConstraint,
    "clear_space_constraints": ClearSpaceConstraints, "ask_user": AskUser,
}


def make_executor(run, context: dict, resume: bool = False):
    """Bind the trusted context and domain services to one Graph tool call."""
    run_id, session_id = run.run_id, run.session_id
    async def execute(name: str, args: dict, step_id: str) -> dict:
        if name in {"list_training", "propose_training"}:
            from cloud.agents.common.training.tools import execute_training
            if run.parent_run_id or context.get("peer_request_id"):
                return {"result": "rejected", "error": "direct_user_request_required"}
            return await execute_training(name, args, {**context, "run_id": run_id})
        if context.get("peer_request_id") and peer_coordination_cancelled(context["peer_request_id"]):
            return {"result": "cancelled", "error": "higher_priority_user_request"}
        if context.get("expires_at") and context["expires_at"] <= time.time():
            return {"result": "timeout", "error": "session_expired"}
        if name in {"invoke_skill", "set_space_constraint",
                    "clear_space_constraints"} and any(
                        step.get("tool") == "start_activity" and
                        (step.get("result") or {}).get("result") == "ok"
                        for step in run.steps):
            return {"result": "rejected", "error": "activity_started_for_this_intent"}
        if name == "list_agents":
            return {"result": "ok", "agents": _public_view(_cards(context))}
        if name == "get_space_context":
            if args:
                return {"result": "rejected", "error": "invalid_arguments"}
            return {"result": "ok", "space": get_space_context(
                "cloud_orchestrator", state=transport._state, registry=transport._registry)}
        if name == "start_activity":
            if (args or resume or context.get("origin_kind", "user_delegated") != "user_delegated"
                    or context.get("peer_request_id") or not context.get("event_id")
                    or type(context.get("user_revision")) is not int):
                return {"result": "rejected", "error": "verified_current_user_intent_required"}
            if any(step.get("tool") == "start_activity" and step.get("step_id") != step_id
                   for step in run.steps):
                return {"result": "rejected", "error": "activity_already_started_for_intent"}
            try:
                started = await invoke_blocking(
                    transport.do_start_activity, context, run.goal,
                    context["event_id"], context["user_revision"],
                    min(12.0, max(1.0, run.deadline - time.time())))
            except Exception as exc:
                logger.warning("activity start failed run=%s error=%s", run_id, type(exc).__name__)
                return {"result": "rejected", "error": "activity_start_unavailable_or_stale"}
            return {"result": "ok", "activity": started["activity"]}
        if name == "send_message":
            started = next((step.get("result") or {} for step in run.steps
                            if step.get("tool") == "start_activity"
                            and (step.get("result") or {}).get("result") == "ok"), None)
            if started is not None:
                activity = started.get("activity") or {}
                target = args.get("target")
                if (context.get("origin_kind") != "user_delegated"
                        or target not in (activity.get("devices") or {})
                        or (activity["devices"][target]).get("status") != "joined"
                        or any(step.get("tool") == "send_message" for step in run.steps)):
                    return {"result": "rejected", "error": "activity_message_denied"}
            messenger = getattr(transport, "_messenger", None)
            if messenger is None:
                return {"result": "error", "error": "message_bus_unavailable"}
            return messenger.send_message(
                "cloud_orchestrator", args.get("target"), args.get("content"),
                args.get("reply_to"),
                activity_id=(started.get("activity") or {}).get("activity_id")
                if started is not None else None)
        if name == "set_space_constraint":
            if context.get("origin_kind", "user_delegated") != "user_delegated":
                return {"result": "rejected", "error": "verified_user_required"}
            text = args.get("text")
            targets = args.get("targets")
            ttl = args.get("ttl_s", 300)
            if (not isinstance(text, str) or text not in run.goal or len(text) > 400
                    or not isinstance(targets, list) or not targets
                    or type(ttl) is not int or not 1 <= ttl <= 3600):
                return {"result": "rejected", "error": "invalid_shared_constraint"}
            allowed = {peer["unit_id"] for peer in get_space_context(
                "cloud_orchestrator", state=transport._state,
                registry=transport._registry)["peers"]} | {"*"}
            if any(not isinstance(target, str) or target not in allowed for target in targets):
                return {"result": "rejected", "error": "invalid_constraint_targets"}
            now = time.time()
            item = {"constraint_id": f"{run_id}_current", "text": text,
                    "source_session_id": session_id, "space_id": DEFAULT_SPACE_ID,
                    "targets": targets, "issued_at": now,
                    "expires_at": min(now + ttl, float(context.get("expires_at") or now + ttl))}
            store = get_constraint_store()
            if not store.upsert(item):
                return {"result": "rejected", "error": "invalid_shared_constraint"}
            if not store.publish_current(transport._mqtt):
                store.remove(item["constraint_id"])
                return {"result": "error", "error": "constraint_publish_failed"}
            return {"result": "ok", "constraint_id": item["constraint_id"],
                    "expires_at": item["expires_at"]}
        if name == "clear_space_constraints":
            if context.get("origin_kind", "user_delegated") != "user_delegated" or args:
                return {"result": "rejected", "error": "verified_user_required"}
            store = get_constraint_store()
            store.remove_session(session_id)
            if not store.publish_current(transport._mqtt):
                return {"result": "error", "error": "constraint_publish_failed"}
            return {"result": "ok"}
        if name == "read_state":
            return {"result": "ok", "sensors": transport._state.sensor_snapshot() if transport._state else {},
                    "actuators": transport._state.actuator_snapshot() if transport._state else {}}
        if name == "invoke_skill":
            if args.get("unit_id") == "go2" and args.get("skill_id") == "go2_move" and any(
                step.get("tool") == "invoke_skill"
                and (step.get("params") or {}).get("unit_id") == "go2"
                and (step.get("params") or {}).get("skill_id") in {"go2_visual_navigate", "go2_find_object"}
                and (step.get("result") or {}).get("result") != "ok"
                for step in run.steps[:-1]
            ):
                return {"result": "blocked", "error": "visual_navigation_fallback_denied"}
            return await invoke_blocking(_invoke_skill, context, args, step_id, run.deadline)
        if name == "await_result":
            task_id = args.get("task_id")
            original = next((s.get("result") for s in run.steps
                             if s.get("tool") == "invoke_skill"
                             and (s.get("result") or {}).get("task_id") == task_id), None)
            if not isinstance(task_id, str) or original is None:
                return {"result": "rejected", "error": "unknown_task_id"}
            endpoint = original.get("query_endpoint")
            if endpoint:
                return await invoke_blocking(
                    transport.do_http_task_status, endpoint, task_id, context,
                    min(5, max(0.1, run.deadline - time.time())))
            return await invoke_blocking(
                _wait_mqtt_result, task_id, session_id, min(run.deadline, time.time() + 20))
        return {"result": "rejected", "error": "unknown_tool"}
    return execute

from cloud.agents.common.training.tools import SCHEMAS as TRAINING_SCHEMAS
STEWARD_TOOL_SCHEMAS.update(TRAINING_SCHEMAS)
