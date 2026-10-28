"""Short, authenticated, single-space messages between cloud agents.

The MQTT callback only validates and records messages. Agent decisions run in a
small worker pool, so a slow model never blocks MQTT status or task results.
Messages are runtime state, not durable commands: an expired message is never
replayed as a physical action after a restart.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from cloud.mqtt_signing import MqttSignatureError, NonceReplayGuard, sign_message, verify_message
from cloud.peers.view import list_peers

_TOPIC = re.compile(r"ssm/agents/([A-Za-z0-9_-]{1,64})/message")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_CONTENT = 1000
_MAX_PENDING = 256
_MAX_ROUNDS = 8
_TERMINAL = {"ok", "blocked", "error", "cancelled", "rejected", "unknown"}
_REPLY_KINDS = {"reply", "accepted", "declined", "question"}


class AgentMessenger:
    """One instance per MQTT process; local_unit_ids identify its agent inboxes."""

    def __init__(
        self, mqtt_client: Any, registry: Any, space_registry: Any,
        local_unit_ids: set[str], on_message: Callable[[dict], None],
        *, task_result_lookup: Callable[[str], dict | None] | None = None,
        on_visible_message: Callable[[dict], None] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._mqtt = mqtt_client
        self._registry = registry
        self._space = space_registry
        self._local_ids = set(local_unit_ids)
        self._on_message = on_message
        self._on_visible_message = on_visible_message
        self._task_result_lookup = task_result_lookup
        self._now = now
        self._lock = threading.RLock()
        self._replay = NonceReplayGuard()
        self._outgoing: dict[str, dict] = {}
        self._inbound: dict[str, dict] = {}
        self._seen: dict[str, float] = {}
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ssm-message")

    def close(self) -> None:
        self._workers.shutdown(wait=False, cancel_futures=True)

    def _peers(self, sender: str) -> dict[str, dict]:
        return {item["unit_id"]: item for item in list_peers(
            self._registry, self._space, initiator_unit_id=sender, now=self._now())}

    def _allowed(self, sender: str, target: str) -> bool:
        if (not isinstance(sender, str) or not isinstance(target, str)
                or sender == target or not _ID.fullmatch(sender) or not _ID.fullmatch(target)):
            return False
        peer = self._peers(sender).get(target)
        return bool(peer and peer["online"])

    def _trim(self, now: float) -> None:
        for message_id, seen_at in tuple(self._seen.items()):
            if seen_at < now - 600:
                self._seen.pop(message_id, None)
        for table in (self._outgoing, self._inbound):
            for message_id, item in tuple(table.items()):
                if item["deadline"] < now - 600:
                    table.pop(message_id, None)
            while len(table) > _MAX_PENDING:
                table.pop(next(iter(table)))

    def _publish(self, payload: dict) -> bool:
        topic = f"ssm/agents/{payload['target']}/message"
        try:
            signed = sign_message(topic, payload)
            info = self._mqtt.publish(topic, json.dumps(signed, ensure_ascii=False), qos=1)
            return getattr(info, "rc", 0) == 0
        except (MqttSignatureError, ValueError, TypeError, OSError):
            return False

    def send_message(
        self, sender: str, target: str, content: str, reply_to: str | None = None,
        *, conversation_id: str | None = None, deadline: float | None = None,
        reply_kind: str = "reply",
    ) -> dict:
        now = self._now()
        if sender not in self._local_ids:
            return {"result": "blocked", "error": "sender_not_local"}
        if not isinstance(content, str) or not content.strip() or len(content) > _MAX_CONTENT:
            return {"result": "blocked", "error": "invalid_content"}
        if reply_kind not in _REPLY_KINDS:
            return {"result": "blocked", "error": "invalid_reply_kind"}
        if not self._allowed(sender, target):
            return {"result": "blocked", "error": "target_offline_or_outside_space"}
        if (deadline is not None and (isinstance(deadline, bool)
                or not isinstance(deadline, (int, float)) or not math.isfinite(deadline))):
            return {"result": "blocked", "error": "invalid_deadline"}
        expires = min(now + 300, deadline if deadline is not None else now + 60)
        if expires <= now:
            return {"result": "blocked", "error": "message_expired"}
        with self._lock:
            self._trim(now)
            parent = self._inbound.get(reply_to) if reply_to else None
            if reply_to and (parent is None or parent["sender"] != target
                             or parent["target"] != sender or parent["deadline"] <= now):
                return {"result": "blocked", "error": "invalid_reply_to"}
            if parent:
                conversation_id = parent["conversation_id"]
                round_number = parent["round"] + 1
            else:
                round_number = 1
            if round_number > _MAX_ROUNDS:
                return {"result": "blocked", "error": "conversation_limit"}
            if conversation_id is not None and (not isinstance(conversation_id, str)
                                                or not _ID.fullmatch(conversation_id)):
                return {"result": "blocked", "error": "invalid_conversation_id"}
            message_id = uuid.uuid4().hex
            payload = {
                "kind": "message", "message_id": message_id,
                "conversation_id": conversation_id or message_id,
                "reply_to": reply_to, "reply_kind": reply_kind,
                "sender": sender, "target": target, "content": content.strip(),
                "deadline": expires, "round": round_number,
            }
            from cloud.space.activity import get_activity_store
            activity = get_activity_store().current()
            if activity:
                payload["activity_id"] = activity["activity_id"]
            if not self._publish(payload):
                return {"result": "error", "error": "mqtt_publish_failed"}
            self._outgoing[message_id] = {**payload, "state": "waiting_reply", "received": False,
                                          "task_id": None, "result": None, "updated_at": now}
            activity_event = None
            if payload.get("activity_id") and "cloud_orchestrator" not in self._local_ids:
                from cloud.space.activity import get_activity_store
                activity_event = get_activity_store().record_event(
                    payload["activity_id"], "message", unit_id=sender,
                    text=payload["content"], target_unit_id=target,
                    message_id=message_id, conversation_id=payload["conversation_id"],
                    reply_to=reply_to, reply_kind=reply_kind)
            if activity_event is None and self._on_visible_message is not None:
                self._on_visible_message(dict(payload))
        return {"result": "accepted", "message_id": message_id,
                "conversation_id": payload["conversation_id"], "state": "waiting_reply"}

    def _send_update(self, message_id: str, *, kind: str, task_id: str | None = None,
                     result: str | None = None, reason: str | None = None) -> dict:
        now = self._now()
        with self._lock:
            original = self._inbound.get(message_id)
            if original is None or (original["deadline"] <= now and
                (kind != "result" or not original.get("task_id") or
                 original["deadline"] < now - 600)):
                return {"result": "blocked", "error": "message_missing_or_expired"}
            if kind == "progress" and (not isinstance(task_id, str) or not _ID.fullmatch(task_id)):
                return {"result": "blocked", "error": "invalid_task_id"}
            if kind == "result" and result not in _TERMINAL:
                return {"result": "blocked", "error": "invalid_result"}
            if kind == "result" and original.get("task_id") and task_id != original["task_id"]:
                return {"result": "blocked", "error": "task_id_mismatch"}
            payload = {
                "kind": kind, "message_id": message_id,
                "conversation_id": original["conversation_id"],
                "sender": original["target"], "target": original["sender"],
                "deadline": max(original["deadline"], now + 60),
            }
            if task_id:
                payload["task_id"] = task_id
            if result:
                payload["result"] = result
            if reason:
                payload["reason"] = str(reason)[:200]
            if not self._publish(payload):
                return {"result": "error", "error": "mqtt_publish_failed"}
            if kind == "progress":
                original["task_id"] = task_id
        return {"result": "accepted", "message_id": message_id}

    def send_progress(self, message_id: str, task_id: str) -> dict:
        """Report the receiving agent's accepted task ID, never the dispatch ID."""
        return self._send_update(message_id, kind="progress", task_id=task_id)

    def send_result(self, message_id: str, result: str, *, task_id: str | None = None,
                    reason: str | None = None) -> dict:
        return self._send_update(message_id, kind="result", task_id=task_id,
                                 result=result, reason=reason)

    def get_status(self, message_id: str) -> dict | None:
        now = self._now()
        with self._lock:
            item = self._outgoing.get(message_id)
            if item is None:
                return None
            if item["deadline"] <= now and item["state"] in {
                "waiting_reply", "waiting_start", "waiting_result"
            }:
                if item["state"] == "waiting_reply":
                    item["state"] = "unanswered"
                elif item["state"] == "waiting_start":
                    item["state"] = "unconfirmed_start"
                else:
                    task_id = item["task_id"]
                    known = self._task_result_lookup(task_id) if self._task_result_lookup and task_id else None
                    item["state"] = (known or {}).get("result", "unknown")
                    item["result"] = known
                item["updated_at"] = now
            return {key: item.get(key) for key in (
                "message_id", "conversation_id", "sender", "target", "state", "received",
                "task_id", "result", "deadline", "updated_at")}

    def handle_message(self, topic: str, raw: bytes | str) -> bool:
        """Return whether this topic is an agent message, even if rejected."""
        match = _TOPIC.fullmatch(topic)
        if match is None:
            return False
        target = match.group(1)
        if target not in self._local_ids:
            return True
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if len(raw) > 5000:
                return True
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return True
            payload = verify_message(topic, payload, replay_guard=self._replay)
        except (UnicodeError, ValueError, TypeError, MqttSignatureError):
            return True
        now = self._now()
        kind = payload.get("kind")
        sender = payload.get("sender")
        message_id = payload.get("message_id")
        conversation_id = payload.get("conversation_id")
        deadline = payload.get("deadline")
        if (kind not in {"message", "received", "progress", "result"}
                or not isinstance(sender, str) or not _ID.fullmatch(sender)
                or payload.get("target") != target
                or not isinstance(message_id, str) or not _ID.fullmatch(message_id)
                or not isinstance(conversation_id, str) or not _ID.fullmatch(conversation_id)
                or isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not now < deadline <= now + 300
                or not self._allowed(target, sender)):
            return True
        with self._lock:
            self._trim(now)
            if kind == "message":
                content = payload.get("content")
                reply_to = payload.get("reply_to")
                round_number = payload.get("round")
                if (not isinstance(content, str) or not content.strip() or len(content) > _MAX_CONTENT
                        or payload.get("reply_kind") not in _REPLY_KINDS
                        or type(round_number) is not int or not 1 <= round_number <= _MAX_ROUNDS
                        or (reply_to is not None and (not isinstance(reply_to, str) or not _ID.fullmatch(reply_to)))):
                    return True
                if message_id in self._seen:
                    return True
                if reply_to:
                    prior = self._outgoing.get(reply_to)
                    if (prior is None or prior["target"] != sender
                            or prior["conversation_id"] != conversation_id
                            or round_number != prior["round"] + 1):
                        return True
                    if prior["state"] not in _TERMINAL:
                        prior["state"] = {"accepted": "waiting_start", "declined": "rejected"}.get(
                            payload["reply_kind"], "answered")
                    prior["updated_at"] = now
                elif round_number != 1 or conversation_id != message_id:
                    return True
                self._seen[message_id] = now
                self._inbound[message_id] = dict(payload)
                activity_event = None
                if (payload.get("activity_id") and "cloud_orchestrator" not in self._local_ids
                        and sender not in self._local_ids):
                    from cloud.space.activity import get_activity_store
                    activity_event = get_activity_store().record_event(
                        payload["activity_id"], "message", unit_id=sender,
                        text=content.strip(), target_unit_id=target,
                        message_id=message_id, conversation_id=conversation_id,
                        reply_to=reply_to, reply_kind=payload["reply_kind"])
                if (sender not in self._local_ids and activity_event is None
                        and self._on_visible_message is not None):
                    self._on_visible_message(dict(payload))
                self._publish({"kind": "received", "message_id": message_id,
                               "conversation_id": conversation_id, "sender": target,
                               "target": sender, "deadline": deadline})
                self._workers.submit(self._on_message, dict(payload))
                return True
            prior = self._outgoing.get(message_id)
            if (prior is None or prior["target"] != sender
                    or prior["conversation_id"] != conversation_id):
                return True
            if kind == "received":
                prior["received"] = True
            elif kind == "progress":
                task_id = payload.get("task_id")
                if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
                    return True
                if prior["state"] not in {"waiting_reply", "waiting_start", "waiting_result"}:
                    return True
                if prior["task_id"] not in (None, task_id):
                    return True
                prior["task_id"], prior["state"] = task_id, "waiting_result"
                if prior.get("activity_id") and "cloud_orchestrator" not in self._local_ids:
                    from cloud.space.activity import get_activity_store
                    get_activity_store().record_event(
                        prior["activity_id"], "action_progress", unit_id=sender,
                        text="设备已接收动作请求", task_id=task_id,
                        message_id=message_id, status="accepted")
            else:
                result = payload.get("result")
                task_id = payload.get("task_id")
                if result not in _TERMINAL or (task_id is not None and
                    (not isinstance(task_id, str) or not _ID.fullmatch(task_id))):
                    return True
                if prior["task_id"] not in (None, task_id):
                    return True
                prior["task_id"], prior["state"], prior["result"] = (
                    task_id or prior["task_id"], result, {"result": result, "reason": payload.get("reason")})
                if prior.get("activity_id") and "cloud_orchestrator" not in self._local_ids:
                    from cloud.space.activity import get_activity_store
                    get_activity_store().record_event(
                        prior["activity_id"], "action_result" if (task_id or prior["task_id"]) else "response",
                        unit_id=sender,
                        text=str(payload.get("reason") or result),
                        task_id=task_id or prior["task_id"],
                        message_id=message_id, status=result)
            prior["updated_at"] = now
            if kind in {"progress", "result"}:
                self._workers.submit(self._on_message, dict(payload))
        return True
