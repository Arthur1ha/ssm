# shared_state.py — Thread-safe store for MQTT ↔ LangGraph data exchange

import threading
import json
from typing import Optional


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._sensors            = {}   # unit_id → { state, event, report }
        self._actuators          = {}   # unit_id → { state, report }
        self._manifests          = {}   # unit_id → manifest payload
        self._decision_active    = False
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

    def get_capability_registry(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._capability_registry))

    def get_manifest(self, unit_id: str) -> Optional[dict]:
        with self._lock:
            m = self._manifests.get(unit_id)
            if m:
                return json.loads(json.dumps(m))
        # 非 MQTT 注册设备（如 Go2）回落到 devices.json
        try:
            if _DEVICES_FILE.exists():
                return json.loads(_DEVICES_FILE.read_text()).get(unit_id)
        except Exception:
            pass
        return None

    def get_all_devices(self) -> dict:
        """所有已知设备：MQTT 注册 + 文件注册（如 Go2）合并，key = unit_id。"""
        with self._lock:
            devices = json.loads(json.dumps(self._manifests))
        try:
            if _DEVICES_FILE.exists():
                for uid, d in json.loads(_DEVICES_FILE.read_text()).items():
                    if uid not in devices:
                        devices[uid] = d
        except Exception:
            pass
        return devices

    def _is_actuator(self, unit_id: str) -> bool:
        return unit_id.split("_")[-1] == "led"
