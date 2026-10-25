"""Authenticated command gateway and per-session SSE event bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from cloud.api.auth import require_csrf, require_session, session_is_active
from cloud.api.policy import audit_event, ensure_go2_action, ensure_instant_action
from cloud.cards.registry import get_registry
from cloud.mqtt_signing import MqttSignatureError, sign_message
from cloud.space.registry import DEFAULT_SPACE_ID, card_device_id, get_space_registry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["gateway"])

_mqtt_client = None


def init_mqtt(client) -> None:
    global _mqtt_client
    _mqtt_client = client


def _service_token() -> str:
    token = os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
    if len(token) < 16:
        raise HTTPException(status_code=503, detail="SSM_INTERNAL_SERVICE_TOKEN is not configured")
    return token


def _context(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session["session_id"],
        "actor": session["actor"],
        "persist": session["actor"] == "admin",
        "space_id": DEFAULT_SPACE_ID,
        "issued_at": session["issued_at"],
        "expires_at": session["expires_at"],
    }


def _mqtt_publish(topic: str, payload: dict[str, Any], *, qos: int = 1) -> None:
    if _mqtt_client is None:
        raise HTTPException(status_code=503, detail="internal message bus is unavailable")
    try:
        info = _mqtt_client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos)
        rc = getattr(info, "rc", 0)
        if rc != 0:
            raise RuntimeError(f"MQTT publish rc={rc}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="internal message bus publish failed") from exc


def _mqtt_publish_signed(topic: str, payload: dict[str, Any], *, qos: int = 1) -> None:
    """Publish an authenticated control message without exposing a reusable token."""
    try:
        signed = sign_message(topic, payload)
    except MqttSignatureError as exc:
        raise HTTPException(
            status_code=503, detail="MQTT control-plane signing is not configured"
        ) from exc
    _mqtt_publish(topic, signed, qos=qos)


def _internal_headers(session: dict[str, Any]) -> dict[str, str]:
    context = _context(session)
    return {
        "Content-Type": "application/json",
        "X-SSM-Service-Token": _service_token(),
        "X-SSM-Session-Id": context["session_id"],
        "X-SSM-Actor": context["actor"],
        "X-SSM-Persist": "true" if context["persist"] else "false",
        "X-SSM-Space-Id": context["space_id"],
        "X-SSM-Origin-Kind": "user_delegated",
        "X-SSM-Expires-At": str(context["expires_at"]),
    }


def _sync_internal_request(
    path: str,
    method: str,
    session: dict[str, Any],
    body: dict[str, Any] | None = None,
) -> Any:
    base = os.getenv("SSM_INTERNAL_API_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        headers=_internal_headers(session),
        method=method,
    )
    try:
        # The API callback must stay on the internal route even when the host
        # exports HTTP_PROXY for outbound traffic. Go2 may need up to 20 s to
        # confirm a motion command, so allow that response to complete.
        direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct_opener.open(request, timeout=25) as response:  # noqa: S310 - configured internal base
            raw = response.read()
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", "internal device request rejected")
        except Exception:
            detail = "internal device request rejected"
        raise HTTPException(status_code=exc.code, detail=detail) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="internal device service unavailable") from exc


async def _internal_request(
    path: str,
    method: str,
    session: dict[str, Any],
    body: dict[str, Any] | None = None,
) -> Any:
    return await asyncio.to_thread(_sync_internal_request, path, method, session, body)


class IntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1, max_length=4000)
    intent_hint: str | None = Field(None, max_length=80)
    device_hint: str | None = Field(None, max_length=120)


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(..., min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)


class _EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, set[queue.Queue]] = defaultdict(set)
        self._buffers: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=100))

    def subscribe(self, session_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            while self._buffers[session_id]:
                channel.put_nowait(self._buffers[session_id].popleft())
            self._queues[session_id].add(channel)
        return channel

    def unsubscribe(self, session_id: str, channel: queue.Queue) -> None:
        with self._lock:
            channels = self._queues.get(session_id)
            if channels:
                channels.discard(channel)
                if not channels:
                    self._queues.pop(session_id, None)

    def emit(self, session_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            channels = list(self._queues.get(session_id, ()))
            if not channels:
                self._buffers[session_id].append(event)
                return
        for channel in channels:
            try:
                channel.put_nowait(event)
            except queue.Full:
                try:
                    channel.get_nowait()
                    channel.put_nowait(event)
                except queue.Empty:
                    pass

    def broadcast(self, event: dict[str, Any]) -> None:
        """Broadcast shared physical-device state to active authenticated SSE clients."""
        with self._lock:
            channels = [channel for group in self._queues.values() for channel in group]
        for channel in channels:
            try:
                channel.put_nowait(dict(event))
            except queue.Full:
                try:
                    channel.get_nowait()
                    channel.put_nowait(dict(event))
                except queue.Empty:
                    pass

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._buffers.pop(session_id, None)


_events = _EventBroker()
_cleanup_tasks: dict[str, asyncio.Task] = {}
_ended_sessions: dict[str, float] = {}


def schedule_session_cleanup(session: dict[str, Any]) -> None:
    """Ensure stateless-cookie sessions still get server-side TTL cleanup.

    This watcher matters when a browser disappears without closing its SSE
    connection cleanly: Go2 tasks and short-term context must not outlive the
    signed session merely because no later HTTP request arrives.
    """
    session_id = str(session["session_id"])
    previous = _cleanup_tasks.get(session_id)
    if previous is not None and not previous.done():
        return

    async def watch() -> None:
        try:
            while session_is_active(session):
                remaining = float(session.get("expires_at", 0)) - time.time()
                await asyncio.sleep(max(0.1, min(15.0, remaining)))
            await end_session(session, reason="expired_or_revoked")
        except asyncio.CancelledError:
            return
        finally:
            if _cleanup_tasks.get(session_id) is asyncio.current_task():
                _cleanup_tasks.pop(session_id, None)

    _cleanup_tasks[session_id] = asyncio.create_task(
        watch(), name=f"session-cleanup-{session_id}"
    )


def _cancel_cleanup_task(session_id: str) -> None:
    task = _cleanup_tasks.pop(session_id, None)
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


def handle_mqtt_message(topic: str, raw: bytes | str) -> bool:
    """Bridge private feedback and current-space device events to SSE."""
    parts = topic.split("/")
    try:
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        payload = raw.decode(errors="replace") if isinstance(raw, bytes) else raw

    if len(parts) == 3 and parts[:2] == ["ssm", "feedback"]:
        session_id = parts[2]
        if not isinstance(payload, dict):
            return True
        payload["session_id"] = session_id
        payload.setdefault("event_id", "evt_" + uuid.uuid4().hex)
        _events.emit(session_id, payload)
        return True

    if len(parts) == 4 and parts[:2] == ["ssm", "agents"]:
        unit_id, message_type = parts[2], parts[3]
        configured = set(get_space_registry().get_space()["device_ids"])
        card = get_registry().get_card(unit_id)
        device_id = card_device_id(card or {"unit_id": unit_id})
        if device_id not in configured and unit_id not in configured:
            return False
        event: dict[str, Any] = {
            "type": "device", "unit_id": unit_id,
            "event_id": "evt_" + uuid.uuid4().hex,
        }
        if message_type in {"card", "manifest"}:
            event["card"] = card
            event["online"] = bool(card and card.get("online", True))
        elif message_type == "status":
            event["online"] = payload == "online"
        elif message_type == "state":
            # Registry applies version-bound runtime normalization before SSE.
            event["state"] = card.get("state", {}) if card else (
                payload if isinstance(payload, dict) else {"value": payload})
        elif message_type == "event":
            event["event"] = payload if isinstance(payload, dict) else {"value": payload}
        elif message_type == "thought":
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                return False
            event["thought"] = payload
        else:
            return False
        _events.broadcast(event)
        return True
    return False


@router.post("/api/intents", status_code=status.HTTP_202_ACCEPTED)
async def create_intent(
    req: IntentRequest,
    session: dict[str, Any] = Depends(require_csrf),
):
    event_id = "evt_" + uuid.uuid4().hex
    payload: dict[str, Any] = {
        "text": req.text,
        "context": _context(session),
        "event_id": event_id,
    }
    if req.intent_hint:
        payload["intent_hint"] = req.intent_hint
    if req.device_hint:
        payload["device_hint"] = req.device_hint
    _mqtt_publish_signed(f"ssm/intent/{session['session_id']}", payload)
    return {"accepted": True, "event_id": event_id, "session_id": session["session_id"]}


def _configured_card(unit_id: str) -> dict[str, Any] | None:
    configured = set(get_space_registry().get_space()["device_ids"])
    card = get_registry().get_card(unit_id)
    if card and card_device_id(card) in configured:
        return card
    return None


def _dispatch_mqtt_task(
    unit_id: str, action: str, params: dict[str, Any], session: dict[str, Any],
    *, source: str | None = None,
) -> str:
    ensure_instant_action(action.upper())
    card = _configured_card(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail="unit is not available in the current space")
    if (card.get("transport") or {}).get("kind") != "mqtt":
        raise HTTPException(status_code=400, detail="unsupported device transport")
    allowed = {
        str((skill.get("invoke") or {}).get("action") or "")
        for skill in card.get("skills", [])
    }
    allowed.update(str(edge.get("action") or "") for edge in (card.get("state_machine") or {}).get("transitions", []))
    if allowed and action not in allowed:
        raise HTTPException(status_code=403, detail="action is not declared by this unit")
    task_id = "task_" + uuid.uuid4().hex
    payload = {
        "task_id": task_id,
        "session_id": session["session_id"],
        "action": action,
        "params": params,
        "actor": session["actor"],
        "persist": session["actor"] == "admin",
        "space_id": DEFAULT_SPACE_ID,
        "ts": int(time.time()),
    }
    if source:
        payload["source"] = source
    _mqtt_publish(f"ssm/task/{unit_id}/{task_id}", payload)
    return task_id


def _valid_lamp_params(action: str, params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if action == "SET_STATE":
        return set(params) == {"state"} and params["state"] in ("ON", "OFF")
    if action == "SET_COLOR":
        required, optional = {"r", "g", "b"}, {"brightness"}
        if not required <= set(params) or set(params) - required - optional:
            return False
        return all(type(value) is int and 0 <= value <= 255 for value in params.values())
    if action == "BLINK":
        if "count" not in params or set(params) - {"count", "r", "g", "b"}:
            return False
        return all(
            type(value) is int and (1 <= value <= 20 if key == "count" else 0 <= value <= 255)
            for key, value in params.items()
        )
    return False


def _run_lamp_chat(agent, unit_id: str, message: str, session: dict[str, Any]) -> dict[str, Any]:
    agent.mark_user_command(unit_id)
    planned = agent.handle_user_text(message, persist=session["actor"] == "admin")
    if not isinstance(planned, dict):
        return {"result": "ok", "reply": str(planned)}
    if planned.get("llm_available") is False:
        return {"result": "error", **planned}
    if planned.get("type") == "peer":
        from cloud.peers.gateway import request_peer_skill
        from cloud.space.execution import ExecutionContext

        context = ExecutionContext(
            session["session_id"], session["actor"], False, DEFAULT_SPACE_ID,
            "user_delegated", int(session["expires_at"]),
        )
        result = request_peer_skill(
            initiator_unit_id=unit_id,
            target_unit_id=planned.get("target_unit_id", ""),
            skill_id=planned.get("skill_id", ""),
            params=planned.get("params", {}),
            context=context,
            origin_kind="user_delegated",
        )
        if result.get("result") == "ok":
            return {"result": "ok", "reply": planned.get("reply") or "同伴任务完成了。",
                    "request_id": result["request_id"]}
        return {"result": result.get("result", "error"), "reply": "同伴没有确认完成这项任务。",
                "error": result.get("error"), "request_id": result.get("request_id")}
    if planned.get("type") != "act":
        return {"result": "ok", **planned}

    action, params = planned.get("action"), planned.get("params")
    if not _valid_lamp_params(action, params):
        logger.warning("lamp chat produced invalid action or params: %s %s", action, params)
        return {"result": "error", "reply": "这条灯光指令我没理解清楚，请再说一次。"}

    task_id = _dispatch_mqtt_task(unit_id, action, params, session, source="lamp_chat")
    execution = agent.wait_task_result(task_id, 20.0)
    outcome = execution.get("result", "error")
    if outcome == "ok":
        return {"result": "ok", "reply": planned["reply"], "action": action, "task_id": task_id}
    detail = execution.get("error") or execution.get("reason")
    logger.warning("lamp chat task %s ended with %s: %s", task_id, outcome, detail)
    reply = "灯没能执行这条命令。" if outcome != "timeout" else "还没收到灯的执行反馈，暂时不能确认它是否执行了。"
    if detail:
        reply += f"原因：{detail}"
    return {"result": outcome, "reply": reply, "task_id": task_id}


async def execute_lamp_chat(unit_id: str, message: str, session: dict[str, Any]) -> dict[str, Any]:
    """理解灯的对话，并在即时命令时等待 ESP32 的执行结果。"""
    if not message.strip():
        raise HTTPException(status_code=400, detail="chat requires message")
    if unit_id != os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led") or _configured_card(unit_id) is None:
        raise HTTPException(status_code=404, detail="unit is not available in the current space")
    from cloud.esp32.agent import get_agent

    agent = get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 agent is unavailable")
    return await asyncio.to_thread(_run_lamp_chat, agent, unit_id, message, session)


async def _dispatch_go2(action: str, params: dict[str, Any], session: dict[str, Any]) -> Any:
    normalized = action.lower()
    admin_routes = {
        "tag_location": ("/api/go2/navigation/locations", "POST", {"name": str(params.get("name") or "").strip()}),
        "set_home": ("/api/go2/navigation/home", "PUT", {}),
        "set_personality": ("/api/go2/personality", "POST", {"prompt": str(params.get("prompt") or "").strip()}),
    }
    if normalized in admin_routes:
        if session["actor"] != "admin":
            raise HTTPException(status_code=403, detail="administrator permission required")
        path, method, body = admin_routes[normalized]
        if normalized == "tag_location" and not body["name"]:
            raise HTTPException(status_code=400, detail="tag_location requires name")
        if normalized == "set_personality" and not body["prompt"]:
            raise HTTPException(status_code=400, detail="set_personality requires prompt")
        return await _internal_request(path, method, session, body)
    if normalized == "delete_location":
        if session["actor"] != "admin":
            raise HTTPException(status_code=403, detail="administrator permission required")
        key = "name"
        value = str(params.get(key) or "").strip()
        if not value:
            raise HTTPException(status_code=400, detail=f"{normalized} requires {key}")
        encoded = urllib.parse.quote(value, safe="")
        return await _internal_request(f"/api/go2/navigation/locations/{encoded}", "DELETE", session)
    if normalized == "velocity":
        ensure_go2_action("VELOCITY")
        return await _internal_request("/api/go2/velocity", "POST", session, params)
    if normalized == "connect":
        return await _internal_request("/api/go2/connection", "POST", session, {})
    if normalized == "disconnect":
        if session["actor"] != "admin":
            raise HTTPException(status_code=403, detail="only an administrator may disconnect Go2")
        return await _internal_request("/api/go2/connection", "DELETE", session)
    if normalized == "set_mode":
        if session["actor"] != "admin":
            raise HTTPException(status_code=403, detail="only an administrator may change autonomy mode")
        if params.get("axis") != "autonomy" or not params.get("value"):
            raise HTTPException(status_code=400, detail="set_mode requires axis=autonomy and value")
        return await _internal_request("/api/go2/autonomy", "PUT", session, {"mode": params["value"]})
    if normalized == "chat":
        message = str(params.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="chat requires message")
        task_id = str(params.get("task_id") or ("go2_" + uuid.uuid4().hex))
        if len(task_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise HTTPException(status_code=400, detail="invalid task_id")
        return await _internal_request("/api/go2/chat", "POST", session, {
            "message": message, "task_id": task_id,
            "deadline": time.time() + 90,
        })

    canonical = action.upper() if action.upper() in {"SPORT", "NAVIGATE", "RECOGNIZE_PERSON", "STOP"} else action
    ensure_go2_action(canonical)
    if canonical == "SPORT":
        return await _internal_request("/api/go2/commands", "POST", session, {"cmd": params.get("cmd", ""), "params": params.get("params", {})})
    if canonical == "NAVIGATE":
        return await _internal_request("/api/go2/navigation/go", "POST", session, {"name": params.get("name", "")})
    if canonical == "RECOGNIZE_PERSON":
        return await _internal_request("/api/go2/identity/recognize", "POST", session, {})
    if canonical == "STOP":
        return await _internal_request("/api/go2/stop", "POST", session, {})
    return await _internal_request("/api/go2/commands", "POST", session, {"action": action, "params": params})


@router.post("/api/commands/{unit_id}")
async def create_command(
    unit_id: str,
    req: CommandRequest,
    response: Response,
    session: dict[str, Any] = Depends(require_csrf),
):
    if "/" in unit_id or not unit_id.strip():
        raise HTTPException(status_code=400, detail="invalid unit id")
    from cloud.peers.gateway import cancel_pending
    cancel_pending(origin_kind="agent_autonomous", reason="user_takeover")
    if unit_id == "go2":
        try:
            result = await _dispatch_go2(req.action, req.params, session)
        except HTTPException as exc:
            if exc.status_code in {400, 403, 409}:
                audit_event("command_rejected", session, unit_id=unit_id, action=req.action, status=exc.status_code)
            raise
        if req.action.lower() in {
            "tag_location", "delete_location", "set_home", "set_personality",
        }:
            audit_event("go2_asset_updated", session, action=req.action)
        if req.action.upper() == "STOP":
            audit_event("emergency_stop", session, reason="device_state_machine")
        if isinstance(result, dict) and result.get("result") == "executing":
            response.status_code = status.HTTP_202_ACCEPTED
        return {"ok": True, "result": result}

    if req.action.lower() == "set_mode":
        if unit_id != os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led") or _configured_card(unit_id) is None:
            raise HTTPException(status_code=404, detail="lamp agent is not available for this unit")
        if session["actor"] != "admin":
            audit_event("command_rejected", session, unit_id=unit_id, action=req.action, status=403)
            raise HTTPException(status_code=403, detail="only an administrator may change autonomy mode")
        if req.params.get("axis") != "autonomy" or not req.params.get("value"):
            raise HTTPException(status_code=400, detail="set_mode requires axis=autonomy and value")
        from cloud.esp32.agent import get_agent

        agent = get_agent()
        if agent is None:
            raise HTTPException(status_code=503, detail="ESP32 agent is unavailable")
        try:
            agent.set_autonomy_mode(str(req.params["value"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "result": {"result": "ok", "mode": agent.get_autonomy_mode()}}

    if req.action.lower() == "chat":
        message = str(req.params.get("message") or "").strip()
        result = await execute_lamp_chat(unit_id, message, session)
        return {"ok": True, "result": result}

    task_id = _dispatch_mqtt_task(unit_id, req.action, req.params, session)
    response.status_code = status.HTTP_202_ACCEPTED
    return {"accepted": True, "status": "dispatched", "task_id": task_id}


@router.get("/api/commands/go2/tasks/{task_id}")
async def get_go2_command_task(task_id: str, session: dict[str, Any] = Depends(require_session)):
    if len(task_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    return await _internal_request(f"/api/go2/tasks/{task_id}", "GET", session)


@router.post("/api/commands/go2/tasks/{task_id}/cancel")
async def cancel_go2_command_task(task_id: str, session: dict[str, Any] = Depends(require_csrf)):
    if len(task_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    return await _internal_request(f"/api/go2/tasks/{task_id}/cancel", "POST", session, {})


@router.post("/api/emergency-stop")
async def emergency_stop(session: dict[str, Any] = Depends(require_csrf)):
    from cloud.peers.gateway import cancel_pending
    cancel_pending(origin_kind="agent_autonomous", reason="user_takeover")
    result = await _internal_request("/api/go2/stop", "POST", session, {})
    audit_event("emergency_stop", session, reason="user_request")
    return {"ok": True, "result": result}


@router.get("/api/events")
async def session_events(session: dict[str, Any] = Depends(require_session)):
    session_id = session["session_id"]
    channel = _events.subscribe(session_id)

    async def generate():
        try:
            yield "event: ready\ndata: {}\n\n"
            while session_is_active(session):
                try:
                    event = await asyncio.to_thread(channel.get, True, 15)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                event_id = str(event.get("event_id") or "evt_" + uuid.uuid4().hex)
                event_name = str(event.get("stage") or event.get("type") or "message")
                yield f"id: {event_id}\nevent: {event_name}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            _events.unsubscribe(session_id, channel)
            if not session_is_active(session):
                await end_session(session, reason="expired_or_revoked")

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


async def end_session(session: dict[str, Any], reason: str) -> None:
    session_id = str(session["session_id"])
    now = time.time()
    for stale_id, ended_at in tuple(_ended_sessions.items()):
        if ended_at < now - 86400:
            _ended_sessions.pop(stale_id, None)
    if session_id in _ended_sessions:
        return
    _ended_sessions[session_id] = now
    _cancel_cleanup_task(session_id)
    from cloud.peers.gateway import cancel_pending
    cancel_pending(session_id=session_id, reason="session_ended")
    context = _context(session)
    payload = {"context": context, "reason": reason}
    if _mqtt_client is not None:
        try:
            _mqtt_publish_signed(f"ssm/session/{session['session_id']}/ended", payload)
        except HTTPException:
            logger.warning("Failed to publish session cleanup for %s", session["session_id"])
    try:
        # Go2 lives in this FastAPI process. Calling its idempotent resource
        # cleanup directly avoids a loopback HTTP dependency during expiry.
        from cloud.go2.router import cleanup_session_resources

        cleanup_session_resources(session_id)
    except Exception:
        logger.warning("Failed to clean Go2 session %s", session_id)
    _events.emit(session_id, {"type": "session_ended", "reason": reason})
    _events.clear(session_id)
    audit_event("session_ended", session, reason=reason)
