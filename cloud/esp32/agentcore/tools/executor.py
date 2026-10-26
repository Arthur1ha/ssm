"""Result-aware execution boundary for a lamp physical action."""

import threading
import time

from cloud.esp32.agentcore.tools.registry import (
    PHYSICAL_ACTIONS, normalize_brightness, valid_auxiliary_params, valid_lamp_params,
)
from cloud.space.execution import ExecutionContext
from cloud.observability import emit_agent_event

_physical_lock = threading.Lock()


def execute_physical(*, state, unit_id: str, action: str, params: dict,
                     context: ExecutionContext, dispatch, wait: bool = True,
                     source: str = "lamp_chat", preflight=None) -> dict:
    """Validate, serialize and track one physical task by its original ID.

    ``dispatch`` is the existing API MQTT publisher; it must return a task ID.
    A publish is only acceptance. The edge result is required for ``ok``.
    """
    if action not in PHYSICAL_ACTIONS or not valid_lamp_params(action, params):
        return {"result": "rejected", "error": "invalid_lamp_action"}
    if (context.space_id != "main" or context.actor not in {"admin", "visitor", "agent"}
            or context.origin_kind not in {"user_delegated", "agent_autonomous"}):
        return {"result": "rejected", "error": "invalid_context"}
    if context.origin_kind == "user_delegated" and context.expires_at <= time.time():
        return {"result": "rejected", "error": "expired_context"}
    if context.origin_kind == "agent_autonomous" and context.persist:
        return {"result": "rejected", "error": "autonomous_persistence_forbidden"}
    with _physical_lock:
        try:
            current = (state.actuator_snapshot().get(unit_id, {}).get("state") or {})
            params = normalize_brightness(action, params, current)
            if preflight is not None:
                block = preflight(current, params)
                if block:
                    return {"result": "blocked", "error": block}
            task_id = dispatch(unit_id, action, params, context, source)
        except ValueError as exc:
            return {"result": "rejected", "error": str(exc)}
        except Exception as exc:
            return {"result": "error", "error": str(exc)}
        if not wait:
            return {"result": "accepted", "task_id": task_id, "action": action}
        outcome = state.wait_task_result(task_id, timeout=PHYSICAL_ACTIONS[action]["timeout"])
        emit_agent_event("device.result", task_id=task_id, target_unit_id=unit_id,
                         action=action, status=outcome.get("result", "error"),
                         result=outcome, completion_basis="edge_result")
        return {"result": outcome.get("result", "error"), "task_id": task_id,
                "action": action, "error": outcome.get("error") or outcome.get("reason")}


def execute_peer_skill(*, initiator_unit_id: str, target_unit_id: str,
                       skill_id: str, params: dict, context: ExecutionContext,
                       mode_available: bool = True, request_id: str | None = None) -> dict:
    if not isinstance(params, dict) or not target_unit_id or not skill_id:
        return {"result": "rejected", "error": "invalid_peer_arguments"}
    if context.space_id != "main" or context.origin_kind not in {
        "user_delegated", "agent_autonomous",
    }:
        return {"result": "rejected", "error": "invalid_context"}
    if context.origin_kind == "user_delegated" and context.expires_at <= time.time():
        return {"result": "rejected", "error": "expired_context"}
    if context.origin_kind == "agent_autonomous" and not mode_available:
        return {"result": "blocked", "error": "autonomy_unavailable"}
    from cloud.peers.gateway import request_peer_skill
    return request_peer_skill(
        initiator_unit_id=initiator_unit_id, target_unit_id=target_unit_id,
        skill_id=skill_id, params=params, context=context,
        origin_kind=context.origin_kind, request_id=request_id,
    )


def execute_auxiliary(*, unit_id: str, tool: str, params: dict,
                      context: ExecutionContext, mode_available: bool = True) -> dict:
    """Execute the lamp's nonphysical tools through one validated boundary."""
    if not valid_auxiliary_params(tool, params):
        return {"result": "rejected", "error": "invalid_tool_arguments"}
    if (context.space_id != "main" or context.actor not in {"admin", "visitor", "agent"}
            or context.origin_kind not in {"user_delegated", "agent_autonomous"}):
        return {"result": "rejected", "error": "invalid_context"}
    if context.origin_kind == "user_delegated" and context.expires_at <= time.time():
        return {"result": "rejected", "error": "expired_context"}
    if context.origin_kind == "agent_autonomous" and (context.persist or not mode_available):
        return {"result": "blocked", "error": "autonomy_unavailable"}
    if tool == "request_peer_skill":
        return execute_peer_skill(
            initiator_unit_id=unit_id, target_unit_id=params["target_unit_id"],
            skill_id=params["skill_id"], params=params["params"], context=context,
            mode_available=mode_available, request_id=params.get("request_id"),
        )
    if tool == "send_message":
        from cloud.api.main import get_agent_messenger
        messenger = get_agent_messenger()
        if messenger is None:
            return {"result": "error", "error": "messaging_unavailable"}
        return messenger.send_message(unit_id, params["target"], params["content"],
                                      reply_to=params.get("reply_to"))
    from cloud.esp32 import tools
    try:
        if tool == "list_peers":
            return {"result": "ok", "peers": tools.list_peers(unit_id)}
        if tool == "get_space_context":
            from cloud.api.main import _esp32_state
            from cloud.space.context import get_space_context
            return {"result": "ok", "space": get_space_context(unit_id, state=_esp32_state)}
        tools.publish_speech(params["text"])
        return {"result": "accepted"}
    except Exception as exc:
        return {"result": "error", "error": str(exc)}
