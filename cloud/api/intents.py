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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from cloud.api.auth import require_csrf, require_session, session_is_active
from cloud.api.policy import audit_event, ensure_go2_action, ensure_instant_action
from cloud.cards.registry import get_registry
from cloud.mqtt_signing import MqttSignatureError, sign_message
from cloud.space.registry import DEFAULT_SPACE_ID, card_device_id, get_space_registry
from cloud.observability import current_event_context, emit_agent_event

logger = logging.getLogger(__name__)
device_chat_logger = logging.getLogger("cloud.esp32.chat")
router = APIRouter(tags=["gateway"])

_mqtt_client = None
_lamp_task_owners: dict[str, tuple[str, str]] = {}
_lamp_task_results: dict[str, dict[str, Any]] = {}
_lamp_task_owners_lock = threading.Lock()


def record_lamp_task_result(task_id: str, result: dict[str, Any]) -> None:
    """Keep owned browser results available even if the state ring is trimmed."""
    with _lamp_task_owners_lock:
        if task_id in _lamp_task_owners:
            _lamp_task_results[task_id] = dict(result)


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
        "persist": session.get("persist", session["actor"] == "admin"),
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
    resume_run_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class CommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(..., min_length=1, max_length=80)
    params: dict[str, Any] = Field(default_factory=dict)


class _EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, set[queue.Queue]] = defaultdict(set)
        self._buffers: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=100))
        self._seen: dict[str, set[str]] = defaultdict(set)
        self._sessions: dict[str, dict[str, Any]] = {}

    def subscribe(self, session: dict[str, Any] | str, last_event_id: str = "") -> queue.Queue:
        legacy = isinstance(session, str)
        if isinstance(session, str):
            session = {"session_id": session, "space_id": DEFAULT_SPACE_ID,
                       "actor": "admin", "expires_at": time.time() + 60}
        session_id = session["session_id"]
        channel: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._sessions[session_id] = dict(session)
            buffered = [] if legacy else list(self._buffers[session_id])
            if last_event_id:
                marker = next((i for i, item in enumerate(buffered)
                               if item.get("event_id") == last_event_id), -1)
                buffered = buffered[marker + 1:] if marker >= 0 else buffered
            for item in buffered:
                channel.put_nowait(item)
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
            event_id = event.get("event_id")
            if event_id in self._seen[session_id]:
                return
            if event_id:
                if len(self._seen[session_id]) >= 200:
                    self._seen[session_id] = {item.get("event_id") for item in self._buffers[session_id]}
                self._seen[session_id].add(event_id)
            self._buffers[session_id].append(dict(event))
            channels = list(self._queues.get(session_id, ()))
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
            self._seen.pop(session_id, None)
            self._sessions.pop(session_id, None)

    def emit_space(self, space_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            sessions = [sid for sid, session in self._sessions.items()
                        if session.get("space_id") == space_id and session_is_active(session)]
        for sid in sessions:
            self.emit(sid, event)


_events = _EventBroker()
_cleanup_tasks: dict[str, asyncio.Task] = {}
_ended_sessions: dict[str, float] = {}
_TYPED_UI_EVENTS = frozenset({"agent_progress", "agent_reply", "reply_error", "run_stage"})


def _sse_event_name(event: dict[str, Any]) -> str:
    """Typed UI events keep their type; stage is a subtype in the payload."""
    event_type = event.get("type")
    if event_type in _TYPED_UI_EVENTS:
        return event_type
    return str(event.get("stage") or event_type or "message")


def publish_agent_event(event_type: str, *, agent_id: str, status: str,
                        session_id: str = "", space_id: str = DEFAULT_SPACE_ID,
                        scope: str = "session", run_id: str = "", trace_id: str = "",
                        parent_run_id: str = "", step_id: str = "", source: str = "device_chat",
                        text: str = "", event_id: str = "", **facts: Any) -> dict[str, Any]:
    """Publish a trusted, structured UI event. Model output can fill text only."""
    if event_type not in {"agent_reply", "agent_progress", "run_stage", "reply_error"}:
        raise ValueError("invalid_event_type")
    if agent_id not in {"cloud_orchestrator", "esp32_desk_led", "go2"}:
        raise ValueError("invalid_agent_id")
    if scope not in {"session", "space"} or (scope == "session" and not session_id):
        raise ValueError("invalid_event_scope")
    if event_type == "agent_reply" and not text.strip():
        return {}
    stable_key = ":".join((event_type, scope, session_id or space_id, run_id,
                           agent_id, step_id, str(facts.get("reply_kind", "final")),
                           str(facts.get("stage", "")), str(facts.get("sequence", ""))))
    event = {"type": event_type, "event_id": event_id or "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, stable_key).hex,
             "agent_id": agent_id, "status": status, "session_id": session_id or None,
             "space_id": space_id, "scope": scope, "run_id": run_id or None,
             "trace_id": trace_id or None, "parent_run_id": parent_run_id or None,
             "step_id": step_id or None, "source": source,
             "text": text.strip(), "ts": time.time(), **facts}
    if scope == "space" and source == "autonomous" and agent_id in {"go2", "esp32_desk_led"}:
        from cloud.space.activity import get_activity_store
        activity = get_activity_store().current(space_id)
        activity_id = activity.get("activity_id") if activity else None
        if activity_id:
            if agent_id == "go2":
                from cloud.go2.router import get_activity_mode_owner
            else:
                from cloud.esp32.router import get_activity_mode_owner
            if get_activity_mode_owner() == activity_id:
                event["activity_id"] = activity_id
                if event_type == "agent_reply":
                    get_activity_store().record_event(activity_id, "message", unit_id=agent_id,
                                                      text=event["text"], status=status)
                elif event_type == "agent_progress" and agent_id == "esp32_desk_led":
                    kind = ("action_result" if status in {"ok", "error", "blocked", "cancelled",
                                                           "timeout", "rejected", "unknown"}
                            else "action_progress")
                    get_activity_store().record_event(activity_id, kind, unit_id=agent_id,
                                                      text=event["text"], status=status,
                                                      task_id=step_id or run_id)
    if scope == "space":
        _events.emit_space(space_id, event)
    else:
        _events.emit(session_id, event)
    return event


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
        if payload.get("type") in {"agent_reply", "agent_progress", "run_stage", "reply_error"}:
            kind = payload["type"]
            trusted = {key: payload.get(key) for key in (
                "agent_id", "status", "run_id", "trace_id", "parent_run_id",
                "step_id", "source", "text", "event_id", "stage", "sequence", "reply_kind",
                "tool", "target_unit_id", "skill_id", "arguments", "requested_text",
                "reason", "result_state",
                "goal", "completed_count", "failed_count", "step_number")}
            if trusted["agent_id"] != "cloud_orchestrator":
                return True
            publish_agent_event(kind, session_id=session_id, scope="session",
                                agent_id="cloud_orchestrator", status=trusted["status"] or "unknown",
                                run_id=trusted["run_id"] or "", trace_id=trusted["trace_id"] or "",
                                parent_run_id=trusted["parent_run_id"] or "",
                                step_id=trusted["step_id"] or "", source="orchestrator",
                                text=trusted["text"] or "", event_id=trusted["event_id"] or "",
                                stage=trusted["stage"], sequence=trusted["sequence"],
                                reply_kind=trusted["reply_kind"] or "final",
                                **{key: trusted[key] for key in ("tool", "target_unit_id", "skill_id",
                                    "arguments", "requested_text", "reason", "result_state", "goal",
                                    "completed_count", "failed_count", "step_number")
                                   if trusted[key] is not None})
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
    trace_id = "tr_" + uuid.uuid4().hex
    if req.resume_run_id:
        from cloud.agent_runtime.store import RunStore
        runs_dir = Path(os.getenv("SSM_ORCHESTRATOR_RUN_DIR",
                                  str(Path(__file__).resolve().parents[1] / "orchestrator" / "data" / "runs")))
        store = RunStore(runs_dir)
        previous = store.get(req.resume_run_id, session["session_id"])
        if previous is not None:
            if previous.trace_id:
                trace_id = previous.trace_id
            else:
                previous.trace_id = trace_id
                store.save(previous)
    from cloud.api.activity import user_intervention
    user_revision = await user_intervention(event_id)
    payload: dict[str, Any] = {
        "text": req.text,
        "context": _context(session),
        "event_id": event_id,
        "trace_id": trace_id,
        "user_revision": user_revision,
    }
    if req.intent_hint:
        payload["intent_hint"] = req.intent_hint
    if req.device_hint:
        payload["device_hint"] = req.device_hint
    if req.resume_run_id:
        payload["resume_run_id"] = req.resume_run_id
    emit_agent_event("task.received", trace_id=trace_id,
                     session_id=session["session_id"], agent_id="orchestrator",
                     origin_kind="user_delegated", input=req.text,
                     entry="/api/intents", event_id=event_id)
    _mqtt_publish_signed(f"ssm/intent/{session['session_id']}", payload)
    return {"accepted": True, "event_id": event_id, "trace_id": trace_id,
            "session_id": session["session_id"], "user_revision": user_revision}


def _configured_card(unit_id: str) -> dict[str, Any] | None:
    configured = set(get_space_registry().get_space()["device_ids"])
    card = get_registry().get_card(unit_id)
    if card and card_device_id(card) in configured:
        return card
    return None


def _dispatch_mqtt_task(
    unit_id: str, action: str, params: dict[str, Any], session: dict[str, Any],
    *, source: str | None = None, task_id: str | None = None,
) -> str:
    ensure_instant_action(action.upper())
    card = _configured_card(unit_id)
    if card is None:
        raise HTTPException(status_code=404, detail="unit is not available in the current space")
    if not card.get("online", False):
        raise HTTPException(status_code=503, detail="unit is offline")
    if unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
        from cloud.esp32.agentcore.tools.registry import (
            valid_lamp_params, normalize_brightness, fsm_allows,
        )
        from cloud.api.main import _esp32_state
        current = (_esp32_state.actuator_snapshot().get(unit_id, {}).get("state") or {})
        current_ism = str(current.get("ism") or (card.get("state") or {}).get("ism") or "")
        legacy_states = {"BRIGHT", "DIM"} <= set((card.get("state_machine") or {}).get("states", []))
        if not valid_lamp_params(action, params, allow_legacy=legacy_states):
            raise HTTPException(status_code=400, detail="invalid lamp action parameters")
        if action == "SET_COLOR" and "brightness" not in params:
            try:
                params = normalize_brightness(action, params, current)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not fsm_allows(card, current_ism, action, params):
            raise HTTPException(status_code=409, detail="lamp action is unavailable in the current FSM state")
    if (card.get("transport") or {}).get("kind") != "mqtt":
        raise HTTPException(status_code=400, detail="unsupported device transport")
    allowed = {
        str((skill.get("invoke") or {}).get("action") or "")
        for skill in card.get("skills", [])
    }
    allowed.update(str(edge.get("action") or "") for edge in (card.get("state_machine") or {}).get("transitions", []))
    if allowed and action not in allowed:
        raise HTTPException(status_code=403, detail="action is not declared by this unit")
    if unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
        from cloud.api.main import _esp32_state
        machine = card.get("state_machine") or {}
        current = (_esp32_state.actuator_snapshot().get(unit_id, {}).get("state") or {}).get("ism")
        transitions = machine.get("transitions") or []
        # Only precheck when the reported state belongs to this Card version.
        # Older retained Cards may use different state names; the edge FSM
        # remains authoritative in that case.
        if current in machine.get("states", []) and transitions and not any(
            edge.get("src") == current and edge.get("action") == action
            for edge in transitions
        ):
            raise HTTPException(status_code=409, detail="action is blocked in the current lamp state")
    task_id = task_id or "task_" + uuid.uuid4().hex
    if not re.fullmatch(r"task_[A-Fa-f0-9]{32}", task_id):
        raise ValueError("invalid_physical_task_id")
    payload = {
        "task_id": task_id,
        "session_id": session["session_id"],
        "action": action,
        "params": params,
        "actor": session["actor"],
        "persist": session.get("persist", session["actor"] == "admin"),
        "space_id": DEFAULT_SPACE_ID,
        "ts": int(time.time()),
    }
    correlation = current_event_context()
    payload.update({key: correlation[key] for key in ("trace_id", "run_id") if key in correlation})
    if source:
        payload["source"] = source
    owned_lamp_task = unit_id == os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led")
    if owned_lamp_task:
        with _lamp_task_owners_lock:
            _lamp_task_owners[task_id] = (unit_id, session["session_id"])
    emit_agent_event("device.command", task_id=task_id, target_unit_id=unit_id,
                     action=action, params=params, transport="mqtt",
                     status="pending_publish")
    try:
        _mqtt_publish(f"ssm/task/{unit_id}/{task_id}", payload)
    except Exception:
        if owned_lamp_task:
            with _lamp_task_owners_lock:
                _lamp_task_owners.pop(task_id, None)
                _lamp_task_results.pop(task_id, None)
        raise
    return task_id


def _valid_lamp_params(action: str, params: Any) -> bool:
    from cloud.esp32.agentcore.tools.registry import valid_lamp_params
    return valid_lamp_params(action, params)


def _run_lamp_chat(agent, unit_id: str, message: str, session: dict[str, Any],
                   run_id: str | None = None, resume: bool = False) -> dict[str, Any]:
    """Adapt an authenticated device-page session to the lamp Planner → Executor."""
    from cloud.space.execution import ExecutionContext

    context = ExecutionContext(
        session["session_id"], session["actor"], session["actor"] == "admin",
        DEFAULT_SPACE_ID, "user_delegated", int(session["expires_at"]),
    )
    def dispatch(uid, action, params, _context, source):
        return _dispatch_mqtt_task(uid, action, params, session, source=source)

    return agent.run_user_loop(message, unit_id, context, dispatch,
                               run_id=run_id, resume=resume)


async def execute_lamp_chat(unit_id: str, message: str, session: dict[str, Any],
                            *, run_id: str | None = None, resume: bool = False) -> dict[str, Any]:
    """理解灯的对话，并在即时命令时等待 ESP32 的执行结果。"""
    if not message.strip():
        raise HTTPException(status_code=400, detail="chat requires message")
    if run_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise HTTPException(status_code=400, detail="invalid run_id")
    card = _configured_card(unit_id)
    if unit_id != os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led") or card is None:
        raise HTTPException(status_code=404, detail="unit is not available in the current space")
    if not card.get("online"):
        raise HTTPException(status_code=503, detail="unit is offline")
    from cloud.esp32.agent import get_agent

    agent = get_agent()
    if agent is None:
        raise HTTPException(status_code=503, detail="ESP32 agent is unavailable")
    device_chat_logger.info(
        "[DeviceChat/User] unit=%s session=%s text=%s",
        unit_id, session["session_id"], json.dumps(message, ensure_ascii=False),
    )
    try:
        result = await run_in_threadpool(_run_lamp_chat, agent, unit_id, message, session, run_id, resume)
    except Exception:
        device_chat_logger.exception(
            "[DeviceChat/Error] unit=%s session=%s", unit_id, session["session_id"],
        )
        raise
    device_chat_logger.info(
        "[DeviceChat/Reply] unit=%s session=%s result=%s text=%s task=%s",
        unit_id, session["session_id"], result.get("result", "ok"),
        json.dumps(result.get("reply", ""), ensure_ascii=False),
        result.get("task_id") or result.get("request_id") or "-",
    )
    return result


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
    from cloud.api.activity import user_intervention
    await user_intervention()
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
        result = await execute_lamp_chat(unit_id, message, session,
                                         run_id=req.params.get("run_id"),
                                         resume=req.params.get("resume") is True)
        return {"ok": True, "result": result}

    task_id = _dispatch_mqtt_task(unit_id, req.action, req.params, session)
    response.status_code = status.HTTP_202_ACCEPTED
    return {"accepted": True, "status": "dispatched", "task_id": task_id}


@router.get("/api/commands/{unit_id}/runs/{run_id}")
async def get_lamp_chat_run(unit_id: str, run_id: str,
                            session: dict[str, Any] = Depends(require_session)):
    if unit_id != os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
        raise HTTPException(status_code=404, detail="lamp run not found")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise HTTPException(status_code=400, detail="invalid run_id")
    from cloud.agent_runtime.store import RunStore
    from cloud.esp32.paths import DATA_DIR
    run = RunStore(DATA_DIR / "runs").get(run_id, session["session_id"])
    if run is None:
        raise HTTPException(status_code=404, detail="lamp run not found")
    if not run.reply_ready:
        return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
                "result": "executing", "reply": "", "question": None,
                "steps": run.steps}
    return {"run_id": run.run_id, "trace_id": run.trace_id or run.run_id,
            "result": run.status, "reply": run.response,
            "event_id": run.reply_event_id or None,
            "question": run.question or None, "steps": run.steps}

@router.get("/api/commands/go2/tasks/{task_id}")
async def get_go2_command_task(task_id: str, session: dict[str, Any] = Depends(require_session)):
    if len(task_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    return await _internal_request(f"/api/go2/tasks/{task_id}", "GET", session)


@router.post("/api/commands/go2/tasks/{task_id}/resume")
async def resume_go2_command_task(task_id: str, req: CommandRequest,
                                  session: dict[str, Any] = Depends(require_csrf)):
    if len(task_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    message = str(req.params.get("message") or "").strip()
    if req.action.lower() != "chat" or not message:
        raise HTTPException(status_code=400, detail="chat requires message")
    from cloud.api.activity import user_intervention
    await user_intervention()
    return await _internal_request(f"/api/go2/tasks/{task_id}/resume", "POST", session,
                                   {"message": message})


@router.get("/api/commands/{unit_id}/tasks/{task_id}")
async def get_lamp_command_task(unit_id: str, task_id: str,
                                session: dict[str, Any] = Depends(require_session)):
    """Query the original lamp physical ID without publishing a second command."""
    if unit_id != os.getenv("ESP32_LED_UNIT_ID", "esp32_desk_led"):
        raise HTTPException(status_code=404, detail="lamp task not found")
    if not re.fullmatch(r"task_[A-Fa-f0-9]{32}", task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    with _lamp_task_owners_lock:
        owner = _lamp_task_owners.get(task_id)
        cached = _lamp_task_results.get(task_id)
    if owner != (unit_id, session["session_id"]):
        raise HTTPException(status_code=404, detail="lamp task not found")
    from cloud.api.main import _esp32_state
    physical = cached or _esp32_state.get_task_result(task_id)
    if physical is None:
        return {"result": "executing", "task_id": task_id}
    return {"result": physical.get("result", "error"), "task_id": task_id,
            "error": physical.get("error") or physical.get("reason")}


@router.post("/api/commands/go2/tasks/{task_id}/cancel")
async def cancel_go2_command_task(task_id: str, session: dict[str, Any] = Depends(require_csrf)):
    if len(task_id) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
        raise HTTPException(status_code=400, detail="invalid task_id")
    from cloud.api.activity import user_intervention
    await user_intervention()
    return await _internal_request(f"/api/go2/tasks/{task_id}/cancel", "POST", session, {})


@router.post("/api/emergency-stop")
async def emergency_stop(session: dict[str, Any] = Depends(require_csrf)):
    from cloud.api.activity import user_intervention
    await user_intervention()
    from cloud.peers.gateway import cancel_pending
    cancel_pending(origin_kind="agent_autonomous", reason="user_takeover")
    result = await _internal_request("/api/go2/stop", "POST", session, {})
    audit_event("emergency_stop", session, reason="user_request")
    return {"ok": True, "result": result}


@router.get("/api/events")
async def session_events(request: Request, session: dict[str, Any] = Depends(require_session)):
    session_id = session["session_id"]
    channel = _events.subscribe(session, request.headers.get("last-event-id", ""))

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
                event_name = _sse_event_name(event)
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
    with _lamp_task_owners_lock:
        for task_id, owner in tuple(_lamp_task_owners.items()):
            if owner[1] == session_id:
                _lamp_task_owners.pop(task_id, None)
                _lamp_task_results.pop(task_id, None)
    audit_event("session_ended", session, reason=reason)
