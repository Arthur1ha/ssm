# shared_state.py — Thread-safe store for MQTT ↔ LangGraph data exchange

import threading
import json
import time
from typing import Optional


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._sensors         = {}   # unit_id → { state, event, report }
        self._actuators       = {}   # unit_id → { state, report }
        self._manifests       = {}   # unit_id → manifest payload（用于按 agent_type 分桶）
        self._decision_active = False
        self._task_results    = {}   # task_id → result payload
        self._task_owners     = {}   # task_id → session_id
        self._sessions        = {}   # session_id → trusted context metadata

    # ── MQTT message ingestion ────────────────────────────────

    def on_manifest(self, unit_id: str, payload: dict):
        with self._lock:
            self._manifests[unit_id] = payload

    def on_agent_msg(self, unit_id: str, msg_type: str, payload: dict):
        with self._lock:
            bucket = self._actuators if self._is_actuator(unit_id) else self._sensors
            if unit_id not in bucket:
                bucket[unit_id] = {}
            bucket[unit_id][msg_type] = payload

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

    def get_session(self, session_id: str) -> Optional[dict]:
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
            owned = [tid for tid, owner in self._task_owners.items() if owner == session_id]
            for task_id in owned:
                self._task_owners.pop(task_id, None)
                self._task_results.pop(task_id, None)

    def register_task(self, task_id: str, session_id: str):
        with self._lock:
            # A task id may be reused by a later turn in the same session.
            self._task_owners[task_id] = session_id
            self._task_results.pop(task_id, None)
            if len(self._task_owners) > 512:
                oldest = next(iter(self._task_owners))
                self._task_owners.pop(oldest, None)
                self._task_results.pop(oldest, None)

    def store_task_result(self, task_id: str, result: dict) -> bool:
        """Store a result only for its registered owning session.

        Older ESP32 firmware may omit session_id; the registered task owner is
        then authoritative.  An explicit mismatching session_id is rejected.
        """
        with self._lock:
            owner = self._task_owners.get(task_id)
            if owner is None:
                return False
            result_session = result.get("session_id")
            if result_session is not None and result_session != owner:
                return False
            if self._task_results.get(task_id, {}).get("result") in ("ok", "blocked", "error", "cancelled", "timeout"):
                return False
            self._task_results[task_id] = {**result, "session_id": owner}
            if len(self._task_results) > 256:
                oldest = next(iter(self._task_results))
                self._task_results.pop(oldest, None)
                self._task_owners.pop(oldest, None)
            return True

    def get_task_result(self, task_id: str, session_id: str | None = None) -> Optional[dict]:
        with self._lock:
            if session_id is not None and self._task_owners.get(task_id) != session_id:
                return None
            return self._task_results.get(task_id)

    def _is_actuator(self, unit_id: str) -> bool:
        """按 manifest agent_type 判定执行器；未知默认非执行器（进 sensor 桶）。"""
        return self._manifests.get(unit_id, {}).get("agent_type") == "actuator"
