import threading
import json
import time
from typing import Optional


class ESP32State:
    def __init__(self):
        self._lock = threading.Lock()
        self._result_ready = threading.Condition(self._lock)
        self._sensors             = {}
        self._actuators           = {}
        self._manifests           = {}
        self._capability_registry = {}
        self._task_results        = {}

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
            bucket.setdefault(unit_id, {})[msg_type] = payload

    def store_task_result(self, task_id: str, result: dict):
        with self._lock:
            previous = self._task_results.get(task_id, {})
            if previous.get("result") in ("ok", "blocked", "error", "cancelled", "timeout"):
                return
            self._task_results[task_id] = result
            if len(self._task_results) > 256:
                self._task_results.pop(next(iter(self._task_results)))
            self._result_ready.notify_all()

    def wait_task_result(self, task_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        with self._result_ready:
            while True:
                result = self._task_results.get(task_id, {})
                if result.get("result") in ("ok", "blocked", "error", "cancelled", "timeout"):
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"task_id": task_id, "result": "timeout"}
                self._result_ready.wait(remaining)

    def get_task_result(self, task_id: str) -> Optional[dict]:
        with self._lock:
            return self._task_results.get(task_id)

    def get_manifest(self, unit_id: str) -> Optional[dict]:
        with self._lock:
            m = self._manifests.get(unit_id)
            return json.loads(json.dumps(m)) if m else None

    def get_capability_registry(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._capability_registry))

    def sensor_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._sensors))

    def actuator_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._actuators))

    def _is_actuator(self, unit_id: str) -> bool:
        return unit_id.split("_")[-1] == "led"
