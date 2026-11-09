# shared_state.py — Thread-safe store for MQTT and the coordinator task loop

import threading
import json
import time


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._sensors         = {}   # unit_id → { state, event, report }
        self._actuators       = {}   # unit_id → { state, report }
        self._manifests       = {}   # unit_id → manifest payload（用于按 agent_type 分桶）
        self._decision_active = False
        self._sessions        = {}   # session_id → trusted context metadata

    # ── MQTT message ingestion ────────────────────────────────

    def on_manifest(self, unit_id: str, payload: dict):
        with self._lock:
            self._manifests[unit_id] = payload

    def on_agent_msg(self, unit_id: str, msg_type: str, payload: dict,
                     *, received_at: float | None = None):
        with self._lock:
            bucket = self._actuators if self._is_actuator(unit_id) else self._sensors
            if unit_id not in bucket:
                bucket[unit_id] = {}
            snapshot = dict(payload)
            snapshot.pop("_received_at", None)
            if received_at is not None:
                snapshot["_received_at"] = received_at
            bucket[unit_id][msg_type] = snapshot

    def set_decision_active(self, active: bool):
        with self._lock:
            self._decision_active = active

    # ── Read helpers ──────────────────────────────────────────

    def sensor_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._sensors))

    def actuator_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._actuators))

    def is_phone_active(self) -> bool:
        with self._lock:
            return self._decision_active

    def register_session(self, context: dict):
        """Remember only server-validated session metadata."""
        session_id = context["session_id"]
        with self._lock:
            self._sessions[session_id] = {
                "session_id": session_id,
                "actor": context["actor"],
                "persist": bool(context["persist"]),
                "space_id": context["space_id"],
                "expires_at": context.get("expires_at"),
            }

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            value = self._sessions.get(session_id)
            return dict(value) if value else None

    def expired_session_ids(self, now: float | None = None) -> list[str]:
        current_time = time.time() if now is None else now
        with self._lock:
            return [
                session_id for session_id, context in self._sessions.items()
                if context.get("expires_at") is not None
                and context["expires_at"] <= current_time
            ]

    def clear_session(self, session_id: str):
        """Remove only one session's ephemeral task/results metadata."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def _is_actuator(self, unit_id: str) -> bool:
        """按 manifest agent_type 判定执行器；未知默认非执行器（进 sensor 桶）。"""
        return self._manifests.get(unit_id, {}).get("agent_type") == "actuator"
