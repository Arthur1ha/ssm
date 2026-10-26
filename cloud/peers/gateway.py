"""Trusted, single-hop peer skill gateway in the API process.

Only in-process agents call this module. MQTT peer requests are signed after
the gateway has checked the source context, target card and skill policy.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from cloud.cards.registry import get_registry
from cloud.mqtt_signing import MqttSignatureError, NonceReplayGuard, sign_message, verify_message
from cloud.space.execution import ExecutionContext
from cloud.space.registry import card_device_id, get_space_registry

logger = logging.getLogger(__name__)
_TERMINAL = {"ok", "blocked", "error", "timeout", "cancelled", "rejected"}
_lock = threading.RLock()
_requests: dict[str, "_Request"] = {}
_mqtt = None
_esp32_state = None
_result_replay_guard = NonceReplayGuard()


@dataclass
class _Request:
    fingerprint: str
    result: dict[str, Any] = field(default_factory=lambda: {"result": "accepted"})
    ready: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    dispatched: bool = False
    context: ExecutionContext | None = None


def init(mqtt_client, esp32_state) -> None:
    global _mqtt, _esp32_state
    _mqtt, _esp32_state = mqtt_client, esp32_state


def shutdown() -> None:
    global _mqtt, _esp32_state
    _mqtt, _esp32_state = None, None


def list_visible_peers(initiator_unit_id: str) -> list[dict[str, Any]]:
    from cloud.peers.view import list_peers

    return list_peers(get_registry(), get_space_registry(), initiator_unit_id=initiator_unit_id)


def _failure(status: str, reason: str, request_id: str) -> dict[str, Any]:
    return {"request_id": request_id, "result": status, "error": reason}


def _validate_schema(schema: dict, params: dict) -> None:
    if schema.get("type") != "object" or not isinstance(params, dict):
        raise ValueError("invalid skill parameters")
    fields = schema.get("properties", {})
    if not isinstance(fields, dict):
        raise ValueError("invalid skill schema")
    if set(schema.get("required", [])) - params.keys():
        raise ValueError("missing required skill parameters")
    if set(params) - set(fields):
        raise ValueError("unknown skill parameters")
    for key, value in params.items():
        spec = fields[key]
        kind = spec.get("type")
        if kind == "string" and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if kind == "integer" and (type(value) is not int):
            raise ValueError(f"{key} must be an integer")
        if kind == "number" and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f"{key} must be a number")
        if kind not in {None, "string", "integer", "number"}:
            raise ValueError("unsupported skill parameter type")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{key} is outside the skill choices")
        if isinstance(value, (int, float)) and type(value) is not bool:
            if "minimum" in spec and value < spec["minimum"]:
                raise ValueError(f"{key} is below minimum")
            if "exclusiveMinimum" in spec and value <= spec["exclusiveMinimum"]:
                raise ValueError(f"{key} is below minimum")
            if "maximum" in spec and value > spec["maximum"]:
                raise ValueError(f"{key} exceeds maximum")
        if isinstance(value, str) and len(value.strip()) < spec.get("minLength", 0):
            raise ValueError(f"{key} is too short")
        if isinstance(value, str) and "maxLength" in spec and len(value) > spec["maxLength"]:
            raise ValueError(f"{key} is too long")


def _trusted_origin(context: ExecutionContext, claimed: str) -> tuple[str, str]:
    if context.space_id != "main":
        raise PermissionError("invalid space context")
    if context.session_id == "system" or context.origin_kind in {"system", "agent_autonomous"}:
        if claimed != "agent_autonomous":
            raise PermissionError("autonomous request cannot claim user delegation")
        return "agent_autonomous", "agent"
    if claimed != "user_delegated" or context.origin_kind != "user_delegated":
        raise PermissionError("origin context mismatch")
    if context.actor not in {"admin", "visitor"} or context.expires_at <= time.time():
        raise PermissionError("user session is expired or unverified")
    return "user_delegated", context.actor


def _authorize_autonomous(initiator: str) -> None:
    if initiator == "go2":
        from cloud.go2 import router
        if router._autonomy_mode == "manual":
            raise PermissionError("Go2 autonomy is manual")
    else:
        from cloud.esp32.agent import get_agent
        agent = get_agent()
        if agent is None or agent.get_autonomy_mode() != "reactive" or agent._in_user_hold():
            raise PermissionError("lamp autonomy is unavailable")


def _skill_policy(initiator: str, target: str, skill_id: str, params: dict, origin: str) -> None:
    if target == "cloud_orchestrator":
        if skill_id != "coordinate_request":
            raise PermissionError("unsupported coordination skill")
        return
    if target == "go2":
        if skill_id == "go2_move" and origin == "user_delegated":
            duration = params.get("duration")
            speed = params.get("speed", 0.3)
            if (params.get("direction") in {"forward", "backward", "left", "right", "turn_left", "turn_right"}
                    and type(duration) in {int, float} and 0 < duration <= 10
                    and type(speed) in {int, float} and 0 < speed <= 0.5):
                return
            raise PermissionError("Go2 peer movement requires bounded direction, duration and speed")
        allowed = {"Hello"} if origin == "agent_autonomous" else {"Hello", "Dance1", "Dance2"}
        if skill_id != "go2_sport" or params.get("cmd") not in allowed:
            raise PermissionError("Go2 peer motion is outside the permitted skill set")
        return
    if skill_id == "adjust_lighting":
        if origin != "user_delegated" or not str(params.get("goal") or "").strip():
            raise PermissionError("semantic lamp request requires user delegation")
        return
    if skill_id == "blink_light":
        if origin != "user_delegated":
            raise PermissionError("autonomous lamp blink is not permitted")
        return
    if skill_id != "set_light_state" or params.get("state") not in {"ON", "OFF"}:
        raise PermissionError("lamp peer request is outside the permitted skill set")


def _find_skill(card: dict, skill_id: str) -> dict | None:
    return next((skill for skill in card.get("skills", []) if skill.get("id") == skill_id), None)


def validate_skill_params(card: dict, skill_id: str, params: dict) -> dict:
    """Resolve and validate a public skill using its current card schema."""
    skill = _find_skill(card, skill_id)
    if skill is None:
        raise PermissionError("skill is not on the current target card")
    _validate_schema(skill.get("params_schema") or {"type": "object", "properties": {}}, params)
    return skill


def _clean_old_requests(now: float) -> None:
    for request_id, entry in tuple(_requests.items()):
        if entry.created_at < now - 3600:
            _requests.pop(request_id, None)


def get_request_result(request_id: str) -> dict[str, Any] | None:
    with _lock:
        entry = _requests.get(request_id)
        if entry is None:
            return None
        result = dict(entry.result)
        context = entry.context
        if (result.get("result") == "timeout" and not result.get("semantic") and result.get("target_unit_id") not in
                {None, "go2", "cloud_orchestrator"} and _esp32_state is not None):
            physical = _esp32_state.get_task_result(request_id)
            if physical and physical.get("result") in _TERMINAL - {"timeout"}:
                result = {**result, "result": physical["result"],
                          "error": physical.get("error") or physical.get("reason")}
                entry.result = result
    if result.get("result") == "timeout" and result.get("semantic") and context is not None:
        endpoint = result.get("endpoint")
        if endpoint:
            latest = _go2_http(f"{endpoint}/{request_id}", "GET", context,
                               "user_delegated", request_id, 2.0)
            if latest.get("result") in _TERMINAL - {"timeout"}:
                with _lock:
                    entry = _requests.get(request_id)
                    if entry is not None and entry.result.get("result") == "timeout":
                        entry.result = {**entry.result, **latest}
                        result = dict(entry.result)
    if (result.get("result") == "timeout" and result.get("target_unit_id") == "go2"
            and context is not None):
        try:
            latest = _go2_task_status(request_id, context,
                                      "agent_autonomous" if context.origin_kind in {"system", "agent_autonomous"}
                                      else "user_delegated", 2.0)
        except RuntimeError:
            latest = {}
        if latest.get("result") in _TERMINAL - {"timeout"}:
            with _lock:
                entry = _requests.get(request_id)
                if entry is not None and entry.result.get("result") == "timeout":
                    entry.result = {**entry.result, **latest}
                    result = dict(entry.result)
    return result


def handle_peer_result(topic: str, raw: bytes | str) -> bool:
    match = re.fullmatch(r"ssm/peer/result/([A-Za-z0-9_-]{1,64})", topic)
    if match is None:
        return False
    try:
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return True
    request_id = match.group(1)
    if not isinstance(payload, dict) or payload.get("request_id") != request_id:
        return True
    try:
        payload = verify_message(topic, payload, replay_guard=_result_replay_guard)
    except MqttSignatureError:
        return True
    with _lock:
        entry = _requests.get(request_id)
        prior = entry.result if entry is not None else {}
        if (entry is None or payload.get("target_unit_id") != "cloud_orchestrator"
                or payload.get("initiator_unit_id") != prior.get("initiator_unit_id")
                or payload.get("result") not in _TERMINAL | {"accepted", "executing"}):
            return True
        late_unknown = prior.get("result") == "timeout" and prior.get("error") == "coordinator_result_unknown"
        if prior.get("result") not in _TERMINAL or late_unknown:
            entry.result = payload
            if payload.get("result") in _TERMINAL:
                entry.ready.set()
            logger.info("[PeerGateway] request=%s coordinator_result=%s",
                        request_id, payload.get("result"))
    return True


def cancel_pending(*, session_id: str | None = None, initiator_unit_id: str | None = None,
                   target_unit_id: str | None = None, origin_kind: str | None = None,
                   reason: str = "user_takeover") -> None:
    coordinator_ids = []
    with _lock:
        for request_id, entry in _requests.items():
            if entry.result.get("result") in _TERMINAL:
                continue
            if session_id and entry.result.get("origin_session_id") != session_id:
                continue
            if initiator_unit_id and entry.result.get("initiator_unit_id") != initiator_unit_id:
                continue
            if target_unit_id and entry.result.get("target_unit_id") != target_unit_id:
                continue
            if origin_kind and entry.result.get("origin_kind") != origin_kind:
                continue
            if not entry.dispatched:
                entry.result = {**entry.result, "result": "cancelled", "error": "origin_cancelled"}
                entry.ready.set()
                logger.info("[PeerGateway] request=%s cancelled before dispatch reason=%s",
                            request_id, reason)
            elif entry.result.get("target_unit_id") == "cloud_orchestrator":
                coordinator_ids.append(request_id)
    for request_id in coordinator_ids:
        if _mqtt is None:
            continue
        topic = f"ssm/peer/cancel/{request_id}"
        payload = {"request_id": request_id, "reason": reason, "ts": int(time.time())}
        try:
            _mqtt.publish(topic, json.dumps(sign_message(topic, payload)), qos=1)
            logger.info("[PeerGateway] request=%s cancellation sent reason=%s",
                        request_id, reason)
        except Exception as exc:
            logger.warning("peer cancellation publish failed request=%s error=%s",
                           request_id, type(exc).__name__)


def request_peer_skill(
    *, initiator_unit_id: str, target_unit_id: str, skill_id: str,
    params: dict[str, Any], context: ExecutionContext, origin_kind: str,
    request_id: str | None = None, timeout: float = 24.0,
) -> dict[str, Any]:
    request_id = request_id or "peer_" + uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request_id):
        return _failure("rejected", "invalid_request_id", request_id)
    try:
        fingerprint = json.dumps([initiator_unit_id, target_unit_id, skill_id, params,
                                  context.session_id, origin_kind], sort_keys=True)
    except (TypeError, ValueError):
        return _failure("rejected", "invalid_parameters", request_id)
    with _lock:
        _clean_old_requests(time.time())
        entry = _requests.get(request_id)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                return _failure("rejected", "request_id_conflict", request_id)
            logger.info("[PeerGateway] request=%s duplicate status=%s",
                        request_id, entry.result.get("result"))
            owner = False
        else:
            entry = _Request(fingerprint)
            entry.context = context
            entry.result = {"request_id": request_id, "result": "accepted",
                            "initiator_unit_id": initiator_unit_id,
                            "target_unit_id": target_unit_id,
                            "origin_kind": origin_kind,
                            "origin_session_id": context.session_id if origin_kind == "user_delegated" else None}
            _requests[request_id] = entry
            owner = True
    if not owner:
        entry.ready.wait(timeout)
        return get_request_result(request_id) or dict(entry.result)

    try:
        origin, actor = _trusted_origin(context, origin_kind)
        if origin == "user_delegated":
            remaining = context.expires_at - time.time()
            if remaining < 0.1:
                raise PermissionError("user session expires before dispatch")
            timeout = min(timeout, remaining)
        if origin == "agent_autonomous":
            _authorize_autonomous(initiator_unit_id)
        space_devices = set(get_space_registry().get_space()["device_ids"])
        cards = get_registry()
        source = cards.get_card(initiator_unit_id)
        target = cards.get_card(target_unit_id)
        if source is None or card_device_id(source) not in space_devices or not source.get("online"):
            raise PermissionError("initiator is unavailable in the current space")
        if target is None or not target.get("online"):
            raise PermissionError("target is offline or unknown")
        if target_unit_id != "cloud_orchestrator" and card_device_id(target) not in space_devices:
            raise PermissionError("target is outside the current space")
        if target_unit_id == initiator_unit_id:
            raise PermissionError("self request is not a peer request")
        skill = validate_skill_params(target, skill_id, params)
        action = str((skill.get("invoke") or {}).get("action") or "")
        if target_unit_id not in {"go2", "cloud_orchestrator"} and skill_id not in {
            "set_light_state", "blink_light", "adjust_lighting",
        }:
            raise PermissionError("peer lamp skill is not permitted")
        _skill_policy(initiator_unit_id, target_unit_id, skill_id, params, origin)
        logger.info("[PeerGateway] request=%s accepted from=%s to=%s skill=%s origin=%s",
                    request_id, initiator_unit_id, target_unit_id, skill_id, origin)
        if entry.result.get("result") == "cancelled":
            return dict(entry.result)
        entry.result = {"request_id": request_id, "result": "executing",
                        "initiator_unit_id": initiator_unit_id,
                        "target_unit_id": target_unit_id,
                        "origin_kind": origin,
                        "origin_session_id": context.session_id if origin == "user_delegated" else None}
        entry.dispatched = True
        if target_unit_id == "go2":
            result = _dispatch_go2(request_id, skill_id, params, context, origin, timeout)
        elif target_unit_id == "cloud_orchestrator":
            result = _dispatch_coordinator(request_id, initiator_unit_id, target_unit_id,
                                           skill_id, params, context, origin, actor, entry, timeout)
        else:
            override = (skill.get("invoke") or {}).get("transport") or {}
            if skill_id == "adjust_lighting" and override.get("kind") == "http":
                from cloud.cards.validation import normalize_card
                if target.get("card_schema_version", 0) != 1:
                    raise PermissionError("legacy card cannot route a service token")
                normalize_card(target)
                result = _dispatch_lamp_semantic(request_id, target_unit_id,
                                                 override["endpoint"], params,
                                                 context, origin, timeout)
            else:
                result = _dispatch_lamp(request_id, target_unit_id, action, params,
                                        context, actor, origin, timeout)
    except PermissionError as exc:
        result = _failure("blocked", str(exc), request_id)
    except ValueError as exc:
        result = _failure("rejected", str(exc), request_id)
    except Exception as exc:
        result = _failure("error", type(exc).__name__, request_id)
    with _lock:
        if result.get("result") == "timeout" and entry.result.get("result") in _TERMINAL - {"timeout"}:
            result = dict(entry.result)
        entry.result = {"request_id": request_id, **result}
        entry.ready.set()
    logger.info("[PeerGateway] request=%s result=%s target=%s error=%s",
                request_id, entry.result.get("result"), target_unit_id,
                entry.result.get("error") or "-")
    return dict(entry.result)


def _dispatch_lamp(request_id: str, target: str, action: str, params: dict,
                   context: ExecutionContext, actor: str, origin: str, timeout: float) -> dict:
    if _mqtt is None or _esp32_state is None:
        return _failure("error", "mqtt_unavailable", request_id)
    payload = {"task_id": request_id, "request_id": request_id,
               "session_id": context.session_id if origin == "user_delegated" else "agent_auto",
               "action": action, "params": params, "actor": actor, "persist": False,
               "space_id": "main", "source": "peer_request", "ts": int(time.time())}
    info = _mqtt.publish(f"ssm/task/{target}/{request_id}", json.dumps(payload), qos=1)
    if getattr(info, "rc", 0) != 0:
        return _failure("error", "mqtt_publish_failed", request_id)
    logger.info("[PeerGateway] request=%s dispatched transport=mqtt target=%s task=%s",
                request_id, target, request_id)
    result = _esp32_state.wait_task_result(request_id, timeout=timeout)
    return {"request_id": request_id, "target_unit_id": target, "task_id": request_id,
            "result": result.get("result", "error"),
            "error": result.get("error") or result.get("reason")}


def _dispatch_lamp_semantic(request_id: str, target: str, endpoint: str, params: dict,
                            context: ExecutionContext, origin: str, timeout: float) -> dict:
    """Call the lamp brain once, then query the same semantic ID until terminal."""
    deadline = time.monotonic() + timeout
    body = {"goal": params["goal"], "task_id": request_id,
            "deadline": time.time() + timeout}
    result = _go2_http(endpoint, "POST", context, origin, request_id, timeout, body)
    while result.get("result") == "executing" and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        result = _go2_http(f"{endpoint}/{request_id}", "GET", context, origin,
                           request_id, max(0.1, deadline - time.monotonic()))
    if result.get("result") == "executing":
        result = {"result": "timeout", "error": "lamp_result_unknown"}
    return {"request_id": request_id, "target_unit_id": target,
            "semantic": True, "endpoint": endpoint, **result}


def _go2_http(path: str, method: str, context: ExecutionContext, origin: str,
              request_id: str, timeout: float, body: dict | None = None) -> dict:
    token = os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("internal_service_token_unavailable")
    base = os.getenv("SSM_INTERNAL_API_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "X-SSM-Service-Token": token,
        "X-SSM-Session-Id": context.session_id if origin == "user_delegated" else request_id,
        "X-SSM-Actor": context.actor if origin == "user_delegated" else "visitor",
        "X-SSM-Persist": "false",
        "X-SSM-Space-Id": "main",
        "X-SSM-Origin-Kind": origin,
        "X-SSM-Expires-At": str(context.expires_at),
    }
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            return None

    # urllib follows redirects by default and may forward internal context
    # headers to the redirected host. Internal service calls stay on one origin.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=max(0.1, min(25.0, timeout))) as response:
            payload = json.loads(response.read())
            return payload if isinstance(payload, dict) else {"result": "error", "error": "invalid_go2_response"}
    except urllib.error.HTTPError as exc:
        return {"result": "blocked" if exc.code in {400, 403, 404, 409} else "error",
                "error": f"go2_http_{exc.code}"}
    except TimeoutError:
        return {"result": "timeout", "error": "go2_result_unknown"}
    except (OSError, ValueError):
        return {"result": "error", "error": "go2_service_unavailable"}


def _go2_task_status(request_id: str, context: ExecutionContext, origin: str,
                     timeout: float) -> dict:
    return _go2_http(f"/api/go2/tasks/{request_id}", "GET", context, origin,
                     request_id, timeout)


def _dispatch_go2(request_id: str, skill_id: str, params: dict,
                  context: ExecutionContext, origin: str, timeout: float) -> dict:
    # Go2's existing HTTP task registry owns idempotency and lease/FSM checks.
    deadline = time.monotonic() + timeout
    body = {"message": "执行公开技能", "skill_id": skill_id, "params": params,
            "task_id": request_id, "deadline": time.time() + timeout,
            "sync_window_s": min(8.0, timeout)}
    logger.info("[PeerGateway] request=%s dispatched transport=http target=go2 skill=%s",
                request_id, skill_id)
    result = _go2_http("/api/go2/chat", "POST", context, origin, request_id, timeout, body)
    while result.get("result") == "executing" and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        result = _go2_task_status(request_id, context, origin,
                                  max(0.1, deadline - time.monotonic()))
    if result.get("result") == "executing":
        result = {"result": "timeout", "error": "go2_result_unknown"}
    return {"request_id": request_id, "target_unit_id": "go2", **result}


def _dispatch_coordinator(request_id: str, initiator: str, target: str, skill: str,
                          params: dict, context: ExecutionContext, origin: str,
                          actor: str, entry: _Request, timeout: float) -> dict:
    if _mqtt is None:
        return _failure("error", "mqtt_unavailable", request_id)
    topic = f"ssm/peer/request/{request_id}"
    now = int(time.time())
    payload = {"request_id": request_id, "initiator_unit_id": initiator,
               "target_unit_id": target, "skill_id": skill, "params": params,
               "origin_kind": origin, "origin_session_id": context.session_id if origin == "user_delegated" else None,
               "actor": actor, "persist": False, "space_id": "main",
               "issued_at": now, "expires_at": now + max(1, math.ceil(timeout))}
    info = _mqtt.publish(topic, json.dumps(sign_message(topic, payload)), qos=1)
    if getattr(info, "rc", 0) != 0:
        return _failure("error", "mqtt_publish_failed", request_id)
    logger.info("[PeerGateway] request=%s dispatched transport=signed_mqtt target=%s",
                request_id, target)
    if not entry.ready.wait(timeout):
        return _failure("timeout", "coordinator_result_unknown", request_id)
    return dict(entry.result)
