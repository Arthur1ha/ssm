# shared_state.py — Thread-safe store for MQTT ↔ LangGraph data exchange

import threading
import json
from typing import Optional


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._sensors   = {}    # unit_id → { state, event, report }
        self._actuators = {}    # unit_id → { state, report }
        self._manifests = {}    # unit_id → manifest payload
        self._decision_active    = False
        self._last_decision      = None
        self._capability_registry = {}  # resource_tag → [unit_id]
        self._task_results        = {}  # task_id → result payload

    # ── MQTT message ingestion ────────────────────────────────

    def on_manifest(self, unit_id: str, payload: dict):
        with self._lock:
            self._manifests[unit_id] = payload
            for tag in payload.get("resource_tags", []):
                if tag not in self._capability_registry:
                    self._capability_registry[tag] = []
                if unit_id not in self._capability_registry[tag]:
                    self._capability_registry[tag].append(unit_id)

    def on_agent_msg(self, unit_id: str, msg_type: str, payload: dict):
        """msg_type: state | event | report"""
        with self._lock:
            bucket = self._actuators if self._is_actuator(unit_id) else self._sensors
            if unit_id not in bucket:
                bucket[unit_id] = {}
            bucket[unit_id][msg_type] = payload

    def set_decision_active(self, active: bool):
        with self._lock:
            self._decision_active = active

    def set_last_decision(self, decision: dict):
        with self._lock:
            self._last_decision = decision

    # ── Read helpers (return copies, safe outside lock) ──────

    def sensor_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._sensors))

    def actuator_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._actuators))

    def last_decision(self):
        with self._lock:
            return self._last_decision

    def is_phone_active(self) -> bool:
        with self._lock:
            return self._decision_active

    def get_command_topic(self, name: str) -> Optional[str]:
        """Find command topic for a named actuator from manifests."""
        with self._lock:
            for m in self._manifests.values():
                if m.get("name") == name and m.get("agent_type") == "actuator":
                    return m.get("topics", {}).get("command")
        return None

    def store_task_result(self, task_id: str, result: dict):
        with self._lock:
            self._task_results[task_id] = result

    def get_task_result(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._task_results.get(task_id)

    def get_capability_registry(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._capability_registry))

    def get_manifest(self, unit_id: str) -> Optional[dict]:
        with self._lock:
            m = self._manifests.get(unit_id)
            return json.loads(json.dumps(m)) if m else None

    def _is_actuator(self, unit_id: str) -> bool:
        suffix = unit_id.split("_")[-1]
        return suffix in ("led", "buz")
