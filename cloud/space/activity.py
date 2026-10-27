"""Current space activity and its bounded, user-visible history.

The retained MQTT value is the shared current-state projection. A small local
journal keeps the activity timeline available after an SSE disconnect.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from cloud.mqtt_signing import MqttSignatureError, verify_message
from cloud.space.registry import DEFAULT_SPACE_ID

CURRENT_ACTIVITY_TOPIC = f"ssm/space/{DEFAULT_SPACE_ID}/activity/current"
_DATA_PATH = Path(__file__).parent / "data" / "activity.json"
_MAX_ACTIVITIES = 4
_MAX_DETAIL_EVENTS = 100
_KEY_EVENTS = {"started", "ended", "device_joined", "device_left", "question"}


class ActivityStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DATA_PATH
        self._lock = threading.RLock()
        self._publisher: Callable[[dict[str, Any]], None] | None = None
        self._event_sink: Callable[[dict[str, Any]], None] | None = None
        self._revision = 0
        self._user_revision = 0
        self._source_event_id = ""
        self._current: dict[str, Any] | None = None
        self._latest_id: str | None = None
        self._records: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._load()

    def configure(self, *, publish: Callable[[dict[str, Any]], None] | None = None,
                  emit: Callable[[dict[str, Any]], None] | None = None) -> None:
        with self._lock:
            self._publisher = publish
            self._event_sink = emit

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            self._revision = int(data.get("revision") or 0)
            self._user_revision = int(data.get("user_revision") or 0)
            self._source_event_id = str(data.get("source_event_id") or "")
            self._current = data.get("current") if isinstance(data.get("current"), dict) else None
            self._latest_id = data.get("latest_id")
            self._records = data.get("records") if isinstance(data.get("records"), dict) else {}
            self._events = data.get("events") if isinstance(data.get("events"), dict) else {}
        except (OSError, ValueError, TypeError):
            return

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"revision": self._revision, "user_revision": self._user_revision,
                "source_event_id": self._source_event_id,
                "current": self._current, "latest_id": self._latest_id,
                "records": self._records, "events": self._events}
        tmp = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)

    def _bump_locked(self) -> int:
        self._revision = max(self._revision + 1, int(time.time() * 1000))
        return self._revision

    def _publish_locked(self) -> None:
        if self._publisher is not None:
            self._publisher({"space_id": DEFAULT_SPACE_ID, "revision": self._revision,
                             "activity": deepcopy(self._current), "ts": time.time()})

    def current(self, space_id: str = DEFAULT_SPACE_ID) -> dict[str, Any] | None:
        if space_id != DEFAULT_SPACE_ID:
            return None
        with self._lock:
            if self._current is None or self._current.get("status") not in {"starting", "active", "partial", "ending"}:
                return None
            return deepcopy(self._current)

    def latest_id(self) -> str | None:
        with self._lock:
            return self._latest_id

    def get(self, activity_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(activity_id)
            return deepcopy(record) if record else None

    def events(self, activity_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events.get(activity_id, []))

    def note_user_intervention(self, source_event_id: str = "") -> int:
        """Record a validated API user request before it reaches a model/device."""
        with self._lock:
            self._user_revision += 1
            self._source_event_id = source_event_id
            self._save_locked()
            return self._user_revision

    def matches_user_request(self, source_event_id: str, user_revision: int) -> bool:
        with self._lock:
            return bool(source_event_id and source_event_id == self._source_event_id
                        and user_revision == self._user_revision)

    def begin(self, text: str, source_event_id: str, user_revision: int,
              source_session_id: str = "", space_id: str = DEFAULT_SPACE_ID) -> dict[str, Any]:
        if space_id != DEFAULT_SPACE_ID or not 1 <= len(text) <= 4000:
            raise ValueError("invalid_activity_context")
        with self._lock:
            if not self.matches_user_request(source_event_id, user_revision):
                raise ValueError("stale_user_request")
            if self._current is not None:
                raise ValueError("activity_already_current")
            activity_id = "act_" + uuid.uuid4().hex
            record = {"activity_id": activity_id, "space_id": space_id,
                      "context": text, "text": text, "source_event_id": source_event_id,
                      "source_session_id": source_session_id, "started_at": time.time(),
                      "revision": self._bump_locked(), "status": "starting", "devices": {}}
            self._current = record
            self._latest_id = activity_id
            self._records[activity_id] = deepcopy(record)
            self._events[activity_id] = []
            while len(self._records) > _MAX_ACTIVITIES:
                stale = next(iter(self._records))
                if stale == activity_id:
                    break
                self._records.pop(stale, None)
                self._events.pop(stale, None)
            self._save_locked()
            self._publish_locked()
            self._record_event_locked(activity_id, "started", text=text)
            return deepcopy(record)

    def set_device(self, activity_id: str, unit_id: str, *, mode: str,
                   status: str, owner: str = "", error: str = "") -> bool:
        with self._lock:
            if not self._current or self._current["activity_id"] != activity_id:
                return False
            if self._current["status"] == "ending":
                return False
            self._current["devices"][unit_id] = {"mode": mode, "status": status,
                                                 "owner": owner, "error": error}
            self._current["revision"] = self._bump_locked()
            self._records[activity_id] = deepcopy(self._current)
            self._save_locked()
            self._publish_locked()
            self._record_event_locked(activity_id, "device_joined" if status == "joined" else "device_left",
                                      unit_id=unit_id, status=status, mode=mode, text=error)
            return True

    def activate(self, activity_id: str) -> dict[str, Any] | None:
        with self._lock:
            if not self._current or self._current["activity_id"] != activity_id or self._current["status"] == "ending":
                return None
            states = [item["status"] for item in self._current["devices"].values()]
            self._current["status"] = "active" if states and all(s == "joined" for s in states) else "partial"
            self._current["revision"] = self._bump_locked()
            self._records[activity_id] = deepcopy(self._current)
            self._save_locked()
            self._publish_locked()
            return deepcopy(self._current)

    def begin_end(self, reason: str = "user_intervention") -> dict[str, Any] | None:
        with self._lock:
            if self._current is None:
                return None
            if self._current["status"] == "ending":
                return deepcopy(self._current)
            self._current["status"] = "ending"
            self._current["end_reason"] = reason
            self._current["revision"] = self._bump_locked()
            activity_id = self._current["activity_id"]
            self._records[activity_id] = deepcopy(self._current)
            self._save_locked()
            self._publish_locked()
            return deepcopy(self._current)

    def finish_end(self, activity_id: str, devices: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self._lock:
            if self._current is None or self._current["activity_id"] != activity_id:
                return None
            self._current["status"] = "ended"
            self._current["ended_at"] = time.time()
            if devices is not None:
                self._current["devices"] = devices
            self._current["revision"] = self._bump_locked()
            ended = deepcopy(self._current)
            self._records[activity_id] = ended
            self._current = None
            self._save_locked()
            self._publish_locked()
            for unit_id, state in ended.get("devices", {}).items():
                if state.get("status") not in {"left", "unknown"}:
                    continue
                physical_stop = state.get("physical_stop", "")
                detail = ("停止已确认" if physical_stop == "confirmed" else
                          "停止结果尚未确认" if physical_stop == "unknown" else
                          state.get("error", ""))
                self._record_event_locked(activity_id, "device_left", unit_id=unit_id,
                                          status=state.get("status"), mode=state.get("mode"),
                                          physical_stop=physical_stop, text=detail)
            self._record_event_locked(activity_id, "ended", text=ended.get("end_reason", ""))
            return ended

    def record_event(self, activity_id: str, event_type: str, *, unit_id: str = "",
                     text: str = "", **facts: Any) -> dict[str, Any] | None:
        with self._lock:
            if not self._current or self._current["activity_id"] != activity_id or self._current["status"] == "ending":
                return None
            return self._record_event_locked(activity_id, event_type, unit_id=unit_id, text=text, **facts)

    def _record_event_locked(self, activity_id: str, event_type: str, *, unit_id: str = "",
                             text: str = "", **facts: Any) -> dict[str, Any]:
        allowed = {"message", "proposal", "response", "observation", "action", "action_progress",
                   "action_result", "question",
                   "started", "ended", "device_joined", "device_left"}
        if event_type not in allowed:
            raise ValueError("invalid_activity_event")
        event = {"type": "activity", "event_type": event_type,
                 "event_id": "evt_" + uuid.uuid4().hex, "activity_id": activity_id,
                 "space_id": DEFAULT_SPACE_ID, "ts": time.time(),
                 "unit_id": unit_id, "text": str(text)[:1000]}
        event.update({key: value for key, value in facts.items()
                      if key in {"target_unit_id", "message_id", "conversation_id", "task_id",
                                 "status", "mode", "media_ref", "reply_to", "reply_kind",
                                 "question_id", "action", "physical_stop", "reason"}})
        rows = self._events.setdefault(activity_id, [])
        rows.append(event)
        details = [index for index, item in enumerate(rows) if item["event_type"] not in _KEY_EVENTS]
        if len(details) > _MAX_DETAIL_EVENTS:
            rows.pop(details[0])
        self._save_locked()
        if self._event_sink is not None:
            self._event_sink(deepcopy(event))
        return deepcopy(event)

    def handle_message(self, topic: str, raw: bytes | str) -> bool:
        if topic != CURRENT_ACTIVITY_TOPIC:
            return False
        try:
            signed = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            # A retained current state can legitimately outlive the ordinary
            # control-message replay window. Revision guards ordering.
            data = verify_message(topic, signed, max_age_s=10 * 365 * 24 * 3600)
            if data.get("space_id") != DEFAULT_SPACE_ID or not isinstance(data.get("revision"), int):
                return True
            with self._lock:
                if data["revision"] <= self._revision:
                    return True
                activity = data.get("activity")
                if activity is not None and not isinstance(activity, dict):
                    return True
                self._revision = data["revision"]
                self._current = deepcopy(activity)
                if activity:
                    self._latest_id = activity.get("activity_id")
                    self._records[self._latest_id] = deepcopy(activity)
        except (ValueError, TypeError, AttributeError, MqttSignatureError):
            pass
        return True


_store = ActivityStore()


def get_activity_store() -> ActivityStore:
    return _store
