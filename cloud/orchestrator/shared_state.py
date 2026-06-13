# shared_state.py — Thread-safe store for MQTT ↔ LangGraph data exchange

import threading
import json
from typing import Optional


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._sensors         = {}   # unit_id → { state, event, report }
        self._actuators       = {}   # unit_id → { state, report }
        self._manifests       = {}   # unit_id → manifest payload（用于按 agent_type 分桶）
        self._decision_active = False
        self._task_results    = {}   # task_id → result payload

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

    def store_task_result(self, task_id: str, result: dict):
        with self._lock:
            self._task_results[task_id] = result

    def get_task_result(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._task_results.get(task_id)

    def _is_actuator(self, unit_id: str) -> bool:
        """按 manifest agent_type 判定执行器；未知默认非执行器（进 sensor 桶）。"""
        return self._manifests.get(unit_id, {}).get("agent_type") == "actuator"
