"""Current, authenticated user requirements shared by members of one space."""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

from cloud.mqtt_signing import MqttSignatureError, sign_message, verify_message
from cloud.space.registry import DEFAULT_SPACE_ID

CURRENT_CONSTRAINTS_TOPIC = f"ssm/space/{DEFAULT_SPACE_ID}/constraints/current"
MAX_CONSTRAINTS = 16
MAX_TEXT = 400
MAX_LIFETIME_S = 3600


def _clean_constraint(value: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    constraint_id = value.get("constraint_id")
    session_id = value.get("source_session_id")
    targets = value.get("targets")
    issued_at, expires_at = value.get("issued_at"), value.get("expires_at")
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT
            or not isinstance(constraint_id, str) or not 1 <= len(constraint_id) <= 100
            or not isinstance(session_id, str) or not 1 <= len(session_id) <= 128
            or value.get("space_id") != DEFAULT_SPACE_ID
            or not isinstance(targets, list) or not targets or len(targets) > 16
            or not all(isinstance(target, str) and 1 <= len(target) <= 120 for target in targets)
            or any(isinstance(number, bool) or not isinstance(number, (int, float))
                   or not math.isfinite(number) for number in (issued_at, expires_at))
            or issued_at > now + 15 or expires_at <= now
            or expires_at <= issued_at or expires_at - issued_at > MAX_LIFETIME_S):
        return None
    return {"constraint_id": constraint_id, "text": text.strip(),
            "source_session_id": session_id, "space_id": DEFAULT_SPACE_ID,
            "targets": list(dict.fromkeys(targets)), "issued_at": float(issued_at),
            "expires_at": float(expires_at)}


class ConstraintStore:
    """Bounded process-local projection of the retained current-state topic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}
        self._revision = 0
        self._snapshot_issued_at = 0

    def _bump_locked(self) -> None:
        # A newly started writer must supersede snapshots from its predecessor.
        self._revision = max(self._revision + 1, int(time.time() * 1000))

    def list_for(self, unit_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
        instant = time.time() if now is None else now
        with self._lock:
            return [dict(item) for item in self._items.values()
                    if item["expires_at"] > instant and
                    ("*" in item["targets"] or unit_id in item["targets"])]

    def upsert(self, item: dict[str, Any], *, now: float | None = None) -> bool:
        """Writer-side update; callers must have a verified user intent."""
        instant = time.time() if now is None else now
        clean = _clean_constraint(item, instant)
        if clean is None:
            return False
        with self._lock:
            self._prune_locked(instant)
            if clean["constraint_id"] not in self._items and len(self._items) >= MAX_CONSTRAINTS:
                self._items.pop(next(iter(self._items)))
            self._items[clean["constraint_id"]] = clean
            self._bump_locked()
        return True

    def remove_session(self, session_id: str) -> bool:
        with self._lock:
            ids = [key for key, item in self._items.items()
                   if item["source_session_id"] == session_id]
            for key in ids:
                self._items.pop(key, None)
            if ids:
                self._bump_locked()
            return bool(ids)

    def remove(self, constraint_id: str) -> bool:
        with self._lock:
            removed = self._items.pop(constraint_id, None) is not None
            if removed:
                self._bump_locked()
            return removed

    def prune_expired(self, *, now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        with self._lock:
            return self._prune_locked(instant)

    def _prune_locked(self, now: float) -> bool:
        ids = [key for key, item in self._items.items() if item["expires_at"] <= now]
        for key in ids:
            self._items.pop(key, None)
        if ids:
            self._bump_locked()
        return bool(ids)

    def publish_current(self, client, *, now: float | None = None) -> bool:
        """Publish one signed retained snapshot; return whether broker accepted it."""
        instant = time.time() if now is None else now
        with self._lock:
            self._prune_locked(instant)
            payload = {"space_id": DEFAULT_SPACE_ID, "revision": self._revision,
                       "issued_at": int(instant), "constraints": list(self._items.values())}
        try:
            signed = sign_message(CURRENT_CONSTRAINTS_TOPIC, payload, now=instant)
            info = client.publish(CURRENT_CONSTRAINTS_TOPIC,
                                  json.dumps(signed, ensure_ascii=False), qos=1, retain=True)
        except (MqttSignatureError, AttributeError, TypeError, ValueError):
            return False
        return getattr(info, "rc", 0) == 0

    def handle_message(self, topic: str, raw: bytes | str, *, now: float | None = None) -> bool:
        if topic != CURRENT_CONSTRAINTS_TOPIC:
            return False
        instant = time.time() if now is None else now
        try:
            payload = json.loads(raw)
            # Retained snapshots may legitimately survive beyond the control-plane
            # default 90 s window; each item still has its own short expiry.
            verified = verify_message(topic, payload, now=instant,
                                      max_age_s=MAX_LIFETIME_S + 60)
        except (ValueError, UnicodeDecodeError, TypeError, MqttSignatureError):
            return False
        if verified.get("space_id") != DEFAULT_SPACE_ID:
            return False
        revision = verified.get("revision")
        issued_at = verified.get("issued_at")
        items = verified.get("constraints")
        if (isinstance(revision, bool) or not isinstance(revision, int)
                or isinstance(issued_at, bool) or not isinstance(issued_at, int)
                or issued_at > instant + 15 or not isinstance(items, list)):
            return False
        if len(items) > MAX_CONSTRAINTS:
            return False
        cleaned = {}
        for item in items:
            clean = _clean_constraint(item, instant)
            if clean:
                cleaned[clean["constraint_id"]] = clean
        with self._lock:
            if (issued_at < self._snapshot_issued_at
                    or (issued_at == self._snapshot_issued_at and revision < self._revision)):
                return False
            self._items = cleaned
            self._revision = revision
            self._snapshot_issued_at = issued_at
        return True


_store = ConstraintStore()


def get_constraint_store() -> ConstraintStore:
    return _store
