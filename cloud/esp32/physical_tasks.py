"""Durable at-most-once cloud dispatch ledger for ESP32 lamp commands.

A reservation is written before MQTT publish. If a process dies after that
write, the original task may or may not have reached the edge; a retry must
query the same ID and may report unknown, never publish it again.
"""

from __future__ import annotations

import hashlib
import json
import math
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
            params: dict, request_params: dict | None = None,
            origin_kind: str | None = None, training: dict | None = None) -> bool:
    """Return True only to the first publisher of this exact task ID.

    The caller must hold no other cross-process lock while publishing.
    An occupied corrupt record raises rather than risking a second dispatch.
    """
    fingerprint = _fingerprint(unit_id, session_id, action, params)
    request_fingerprint = _fingerprint(unit_id, session_id, action,
                                       request_params if request_params is not None else params)
    with locked_directory(PHYSICAL_TASKS_DIR):
        path = record_path(PHYSICAL_TASKS_DIR, task_id)
        old = read_record(path)
        if old is not None:
            same_request = old.get("request_fingerprint") == request_fingerprint
            if not same_request or (origin_kind is not None and
                                    old.get("origin_kind") != origin_kind):
                raise ValueError("physical_task_id_conflict")
            return False
        return write_record(path, {
            "schema_version": 1, "task_id": task_id, "unit_id": unit_id,
            "session_id": session_id, "action": action,
            "fingerprint": fingerprint, "status": "reserved",
            "request_fingerprint": request_fingerprint, "origin_kind": origin_kind,
            "request_params": request_params if request_params is not None else params,
            "params": params,
            "result": None, "created_at": time.time(), "training": training,
        }, exclusive=True)


def record_result(task_id: str, unit_id: str, result: dict) -> bool:
    """Persist only a receipt owned by an existing durable reservation."""
    if (result.get("task_id") not in (None, task_id)
            or result.get("unit_id") not in (None, unit_id)
            or result.get("result") not in {"accepted", "executing", "ok", "blocked",
                                            "rejected", "cancelled", "error", "timeout"}):
        return False
    with locked_directory(PHYSICAL_TASKS_DIR):
        path = record_path(PHYSICAL_TASKS_DIR, task_id)
        old = read_record(path)
        if old is None:
            return False
        if old.get("unit_id") != unit_id:
            return False
        if result.get("session_id") not in (None, old.get("session_id")):
            return False
        previous = old.get("result") or {}
        if previous.get("result") in {"ok", "blocked", "rejected", "cancelled", "error"}:
            return previous["result"] == result.get("result")
        old["result"] = dict(result)
        old["status"] = "observed"
        old["updated_at"] = time.time()
        if result.get("result") == "ok":
            # Server receipt time, never an untrusted device supplied timestamp.
            old["completed_at"] = old["updated_at"]
        write_record(path, old)
        return True


def observed_result(task_id: str) -> dict | None:
    """Internal peer-message lookup of an assigned task's durable observation."""
    with locked_directory(PHYSICAL_TASKS_DIR):
        record = read_record(record_path(PHYSICAL_TASKS_DIR, task_id))
    result = record.get("result") if record else None
    return dict(result) if isinstance(result, dict) else None


def get(task_id: str, *, unit_id: str, session_id: str,
        action: str | None = None, request_params: dict | None = None,
        origin_kind: str | None = None) -> dict | None:
    """Read a session-owned result without causing another MQTT publish."""
    with locked_directory(PHYSICAL_TASKS_DIR):
        old = read_record(record_path(PHYSICAL_TASKS_DIR, task_id))
    if old is None:
        return None
    if old.get("unit_id") != unit_id or old.get("session_id") != session_id:
        if action is not None:
            raise ValueError("physical_task_id_conflict")
        return None
    if action is not None:
        fingerprint = _fingerprint(unit_id, session_id, action, request_params or {})
        if (old.get("request_fingerprint") != fingerprint
                or (origin_kind is not None and old.get("origin_kind") != origin_kind)):
            raise ValueError("physical_task_id_conflict")
    result = old.get("result")
    training = old.get("training") or {}
    attribution = {key: training[key] for key in ("training_refs", "reasons", "transition_id")
                   if key in training}
    return {"task_id": task_id, **attribution, **result} if isinstance(result, dict) else {
        "task_id": task_id, "result": "unknown", "error": "edge_result_unconfirmed",
        **attribution,
    }


def completion_history(*, unit_id: str, transition_id: str, teaching_id: str,
                       since: float, origin_kind: str | None = None,
                       trigger_ref: str | None = None) -> tuple[bool, float | None]:
    """Project confirmed completion times from the authoritative task records."""
    latest = None
    try:
        with locked_directory(PHYSICAL_TASKS_DIR):
            for path in PHYSICAL_TASKS_DIR.glob("*.json"):
                row = read_record(path)
                if row is None or row.get("unit_id") != unit_id:
                    continue
                training = row.get("training") or {}
                if (training.get("transition_id") != transition_id
                        or teaching_id not in training.get("teaching_ids", [])
                        or (origin_kind is not None and row.get("origin_kind") != origin_kind)
                        or (trigger_ref is not None and training.get("trigger_ref") != trigger_ref)
                        or row.get("created_at", 0) < since):
                    continue
                result = row.get("result") or {}
                if result.get("result") in {None, "accepted", "executing", "timeout"}:
                    return False, None
                if result.get("result") == "ok":
                    completed = row.get("completed_at")
                    if (type(completed) not in (int, float) or not math.isfinite(completed)
                            or completed < row.get("created_at", 0)):
                        return False, None
                    latest = max(latest or completed, completed)
    except (OSError, ValueError, TypeError):
        return False, None
    return True, latest
