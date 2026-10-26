"""Durable, idempotent lamp semantic tasks and their physical task IDs.

The JSON task ID ledger has no automatic expiry. Reusing an old ID can
return its recorded result, but can never dispatch another physical command.
"""

import time
import threading
from dataclasses import dataclass, field

from cloud.agent_runtime.json_files import locked_directory, read_record, record_path, write_record
from cloud.esp32.paths import TASKS_DIR


@dataclass
class _Task:
    fingerprint: tuple
    result: dict = field(default_factory=lambda: {"result": "executing"})
    ready: threading.Event = field(default_factory=threading.Event)


_lock = threading.RLock()
_tasks: dict[str, _Task] = {}


def _path(task_id: str):
    return record_path(TASKS_DIR, task_id)


def _read(task_id: str) -> tuple[tuple, dict] | None:
    with locked_directory(TASKS_DIR):
        data = read_record(_path(task_id))
    return (tuple(data["fingerprint"]), data["result"]) if data else None


def _write(task_id: str, fingerprint: tuple, result: dict) -> None:
    with locked_directory(TASKS_DIR):
        path = _path(task_id)
        old = read_record(path)
        if old is not None and tuple(old["fingerprint"]) != fingerprint:
            raise ValueError("task_id_conflict")
        write_record(path, {"schema_version": 1, "task_id": task_id,
                            "fingerprint": list(fingerprint), "result": result,
                            "physical_task_id": result.get("physical_task_id"),
                            "updated_at": time.time()})


def _reserve(task_id: str, fingerprint: tuple, trace_id: str = "") -> bool:
    with locked_directory(TASKS_DIR):
        return write_record(_path(task_id), {
            "schema_version": 1, "task_id": task_id,
            "fingerprint": list(fingerprint),
            "result": {"result": "executing", "task_id": task_id, "trace_id": trace_id},
            "physical_task_id": None, "updated_at": time.time(),
        }, exclusive=True)


def get(task_id: str, session_id: str | None = None) -> dict | None:
    with _lock:
        task = _tasks.get(task_id)
        if task:
            fingerprint, result = task.fingerprint, dict(task.result)
        else:
            stored = _read(task_id)
            if stored is None:
                return None
            fingerprint, result = stored
            if result.get("result") == "executing":
                result = {**result, "result": "timeout", "error": "result_unknown_after_restart"}
        if session_id is not None and fingerprint[0] != session_id:
            return None
        return result


def update_progress(task_id: str, physical_task_id: str) -> None:
    """Record the physical ID before waiting, so crash recovery never resends."""
    with _lock:
        task = _tasks.get(task_id)
        if task and task.result.get("result") == "executing":
            task.result = {**task.result, "physical_task_id": physical_task_id}
            _write(task_id, task.fingerprint, task.result)


def update_late_result(task_id: str, session_id: str, physical: dict) -> dict | None:
    with _lock:
        current = get(task_id, session_id)
        if current is None:
            return None
        if current.get("result") == "timeout" and physical.get("result") in {
            "ok", "blocked", "rejected", "cancelled", "error",
        }:
            result = {**current, "result": physical["result"],
                      "error": physical.get("error") or physical.get("reason")}
            task = _tasks.get(task_id)
            if task:
                task.result = result
            stored = _read(task_id)
            if stored:
                _write(task_id, stored[0], result)
            return result
        return current


def run_once(task_id: str, fingerprint: tuple, work, sync_window_s: float = 25.0,
             *, trace_id: str = "") -> dict:
    with _lock:
        task = _tasks.get(task_id)
        if task is not None:
            if task.fingerprint != fingerprint:
                return {"result": "rejected", "task_id": task_id, "error": "task_id_conflict"}
            owner = False
        else:
            stored = _read(task_id)
            if stored is not None:
                if stored[0] != fingerprint:
                    return {"result": "rejected", "task_id": task_id, "error": "task_id_conflict"}
                # A completed ID may have left the memory cache; an executing
                # ID from a prior process has an unknown outcome. Neither runs again.
                return get(task_id)
            if not _reserve(task_id, fingerprint, trace_id):
                stored = _read(task_id)
                if stored and stored[0] != fingerprint:
                    return {"result": "rejected", "task_id": task_id, "error": "task_id_conflict"}
                return get(task_id)
            task = _Task(fingerprint, {"result": "executing", "task_id": task_id,
                                       "trace_id": trace_id})
            _tasks[task_id] = task
            if len(_tasks) > 256:
                stale = next((key for key, item in _tasks.items()
                              if key != task_id and item.ready.is_set()), None)
                if stale:
                    _tasks.pop(stale, None)
            owner = True
    if owner:
        def run():
            try:
                result = work()
                if not isinstance(result, dict):
                    result = {"result": "error", "error": "invalid_semantic_result"}
            except Exception as exc:
                result = {"result": "error", "error": type(exc).__name__}
            with _lock:
                task.result = {**result, "task_id": task_id,
                               "trace_id": result.get("trace_id") or trace_id}
                _write(task_id, task.fingerprint, task.result)
                task.ready.set()
        threading.Thread(target=run, name=f"lamp-task-{task_id[:16]}", daemon=True).start()
    task.ready.wait(max(0.0, min(sync_window_s, 25.0)))
    return get(task_id) or {"result": "error", "task_id": task_id}
