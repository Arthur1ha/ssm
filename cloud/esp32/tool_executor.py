"""Result-aware execution boundary for a lamp physical action."""

import threading
import time
import uuid

from cloud.esp32.tool_registry import (
    PHYSICAL_ACTIONS, fsm_allows, normalize_brightness, valid_auxiliary_params, valid_lamp_params,
)
from cloud.space.execution import ExecutionContext
from cloud.observability import emit_agent_event

_physical_lock = threading.Lock()


def execute_physical(*, state, unit_id: str, action: str, params: dict,
                     context: ExecutionContext, dispatch, wait: bool = True,
                     source: str = "lamp_chat", preflight=None,
                     task_id: str | None = None, card: dict | None = None,
                     timeout: float | None = None) -> dict:
    """One physical boundary for buttons, Graphs, steward and peers.

    Reserve before invoking the transport callback. The callback accepts the
    original ``task_id`` keyword and never owns another dispatch ledger.
    A retry reads the reservation, including after an uncertain publish.
    """
    from cloud.esp32.physical_tasks import get, reserve, record_result

    legacy = bool(card and {"BRIGHT", "DIM"} <= set(
        (card.get("state_machine") or {}).get("states", [])))
    if action not in PHYSICAL_ACTIONS or not valid_lamp_params(action, params, allow_legacy=legacy):
        return {"result": "rejected", "error": "invalid_lamp_action"}
    if (context.space_id != "main" or context.actor not in {"admin", "visitor", "agent"}
            or context.origin_kind not in {"user_delegated", "agent_autonomous"}):
        return {"result": "rejected", "error": "invalid_context"}
    if context.origin_kind == "user_delegated" and context.expires_at <= time.time():
        return {"result": "rejected", "error": "expired_context"}
    if context.origin_kind == "agent_autonomous" and context.persist:
        return {"result": "rejected", "error": "autonomous_persistence_forbidden"}
    task_id = task_id or "task_" + uuid.uuid4().hex
    request_params = dict(params)
    with _physical_lock:
        reserved_here = False
        try:
            previous = get(task_id, unit_id=unit_id, session_id=context.session_id,
                           action=action, request_params=request_params,
                           origin_kind=context.origin_kind)
            if previous is not None:
                return {**previous, "action": action}
            current = (state.actuator_snapshot().get(unit_id, {}).get("state") or {})
            params = normalize_brightness(action, params, current)
            if card is not None:
                if card.get("unit_id") != unit_id or not card.get("online"):
                    return {"result": "blocked", "error": "lamp_unavailable"}
                if (card.get("transport") or {}).get("kind") != "mqtt":
                    return {"result": "rejected", "error": "unsupported_device_transport"}
                if not any((skill.get("invoke") or {}).get("action") == action
                           for skill in card.get("skills", [])):
                    return {"result": "rejected", "error": "undeclared_lamp_action"}
                current_ism = str(current.get("ism") or (card.get("state") or {}).get("ism") or "")
                if not fsm_allows(card, current_ism, action, params):
                    return {"result": "blocked", "error": "lamp_action_unavailable"}
            if preflight is not None:
                block = preflight(current, params)
                if block:
                    return {"result": "blocked", "error": block}
            first = reserve(task_id, unit_id=unit_id, session_id=context.session_id,
                            action=action, params=params, request_params=request_params,
                            origin_kind=context.origin_kind)
            if not first:
                return {**get(task_id, unit_id=unit_id, session_id=context.session_id), "action": action}
            reserved_here = True
            dispatched = dispatch(unit_id, action, params, context, source, task_id=task_id)
            if dispatched != task_id:
                raise ValueError("physical_dispatch_id_mismatch")
        except ValueError as exc:
            if reserved_here:
                return {**get(task_id, unit_id=unit_id, session_id=context.session_id), "action": action}
            return {"result": "rejected", "error": str(exc), "task_id": task_id}
        except Exception as exc:
            reserved = get(task_id, unit_id=unit_id, session_id=context.session_id)
            if reserved is not None:
                # A transport failure cannot prove the broker rejected a publish.
                return {**reserved, "action": action}
            return {"result": "error", "error": str(exc), "task_id": task_id}
    if not wait:
        return {"result": "accepted", "task_id": task_id, "action": action}
    # Waiting holds no dispatch lock: a new user command may cancel a blink.
    outcome = state.wait_task_result(task_id, timeout=(PHYSICAL_ACTIONS[action]["timeout"]
                                                     if timeout is None else timeout))
    if (outcome.get("task_id") not in (None, task_id)
            or outcome.get("unit_id") not in (None, unit_id)
            or outcome.get("session_id") not in (None, context.session_id)):
        outcome = {"result": "unknown", "error": "edge_result_identity_mismatch"}
    if outcome.get("result") != "timeout":
        record_result(task_id, unit_id, outcome)
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
                                      reply_to=params.get("reply_to"),
                                      interaction_kind=params.get("interaction_kind", "message"),
                                      game_id=params.get("game_id"))
    try:
        if tool == "list_peers":
            from cloud.esp32 import tools
            return {"result": "ok", "peers": tools.list_peers(unit_id)}
        if tool == "get_space_context":
            from cloud.api.main import _esp32_state
            from cloud.space.context import get_space_context
            return {"result": "ok", "space": get_space_context(unit_id, state=_esp32_state)}
    except Exception as exc:
        return {"result": "error", "error": str(exc)}
    return {"result": "rejected", "error": "unknown_auxiliary_tool"}
