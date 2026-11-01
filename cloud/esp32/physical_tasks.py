"""Durable at-most-once cloud dispatch ledger for ESP32 lamp commands.

A reservation is written before MQTT publish. If a process dies after that
write, the original task may or may not have reached the edge; a retry must
query the same ID and may report unknown, never publish it again.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from cloud.storage.json_files import locked_directory, read_record, record_path, write_record
from cloud.esp32.paths import PHYSICAL_TASKS_DIR


def stable_task_id(unit_id: str, run_id: str, step_id: str) -> str:
    """Map one Graph/loop step to the MQTT protocol's task_<32hex> ID."""
    if not all(isinstance(value, str) and value for value in (unit_id, run_id, step_id)):
        raise ValueError("invalid_step_identity")
    return "task_" + uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"ssm/lamp/{unit_id}/{run_id}/{step_id}").hex


def _fingerprint(unit_id: str, session_id: str, action: str, params: dict) -> str:
    payload = json.dumps([unit_id, session_id, action, params],
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def reserve(task_id: str, *, unit_id: str, session_id: str, action: str,
            params: dict) -> bool:
    """Return True only to the first publisher of this exact task ID.

    The caller must hold no other cross-process lock while publishing.
    An occupied corrupt record raises rather than risking a second dispatch.
    """
    fingerprint = _fingerprint(unit_id, session_id, action, params)
    with locked_directory(PHYSICAL_TASKS_DIR):
        path = record_path(PHYSICAL_TASKS_DIR, task_id)
        old = read_record(path)
        if old is not None:
            if old.get("fingerprint") != fingerprint:
                raise ValueError("physical_task_id_conflict")
            return False
        return write_record(path, {
            "schema_version": 1, "task_id": task_id, "unit_id": unit_id,
            "session_id": session_id, "action": action,
            "fingerprint": fingerprint, "status": "reserved",
            "result": None, "created_at": time.time(),
        }, exclusive=True)


def record_result(task_id: str, unit_id: str, result: dict) -> bool:
    """Persist a real edge result; ignore unrelated or already final records."""
    if result.get("task_id") not in (None, task_id):
        return False
    with locked_directory(PHYSICAL_TASKS_DIR):
        path = record_path(PHYSICAL_TASKS_DIR, task_id)
        old = read_record(path)
        if old is None or old.get("unit_id") != unit_id:
            return False
        if result.get("session_id") not in (None, old.get("session_id")):
            return False
        previous = old.get("result") or {}
        if previous.get("result") in {"ok", "blocked", "rejected", "cancelled", "error"}:
            return True
        old["result"] = dict(result)
        old["status"] = "observed"
        old["updated_at"] = time.time()
        write_record(path, old)
        return True


def get(task_id: str, *, unit_id: str, session_id: str) -> dict | None:
    """Read a session-owned result without causing another MQTT publish."""
    with locked_directory(PHYSICAL_TASKS_DIR):
        old = read_record(record_path(PHYSICAL_TASKS_DIR, task_id))
    if old is None or old.get("unit_id") != unit_id or old.get("session_id") != session_id:
        return None
    result = old.get("result")
    return {"task_id": task_id, **result} if isinstance(result, dict) else {
        "task_id": task_id, "result": "unknown", "error": "edge_result_unconfirmed",
    }
