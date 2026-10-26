"""Durable, idempotent lamp semantic tasks and their physical task IDs.

The SQLite task ID ledger has no automatic expiry. Reusing an old ID can
return its recorded result, but can never dispatch another physical command.
"""

import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field

from cloud.esp32.paths import TASKS_FILE


@dataclass
class _Task:
    fingerprint: tuple
    result: dict = field(default_factory=lambda: {"result": "executing"})
    ready: threading.Event = field(default_factory=threading.Event)


_lock = threading.RLock()
_tasks: dict[str, _Task] = {}


def _connect():
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(TASKS_FILE, timeout=5)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS semantic_tasks ("
        "task_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)"
    )
    return connection


def _read(task_id: str) -> tuple[tuple, dict] | None:
    with closing(_connect()) as db:
        row = db.execute("SELECT fingerprint, result FROM semantic_tasks WHERE task_id=?",
                         (task_id,)).fetchone()
    return (tuple(json.loads(row[0])), json.loads(row[1])) if row else None


def _write(task_id: str, fingerprint: tuple, result: dict) -> None:
    with closing(_connect()) as db:
        with db:
            db.execute(
                "INSERT INTO semantic_tasks(task_id,fingerprint,result) VALUES(?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET result=excluded.result",
                (task_id, json.dumps(fingerprint), json.dumps(result, ensure_ascii=False)),
            )


def _reserve(task_id: str, fingerprint: tuple) -> bool:
    with closing(_connect()) as db:
        with db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO semantic_tasks(task_id,fingerprint,result) VALUES(?,?,?)",
                (task_id, json.dumps(fingerprint),
                 json.dumps({"result": "executing", "task_id": task_id})),
            )
            return cursor.rowcount == 1


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


def run_once(task_id: str, fingerprint: tuple, work, sync_window_s: float = 25.0) -> dict:
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
            if not _reserve(task_id, fingerprint):
                stored = _read(task_id)
                if stored and stored[0] != fingerprint:
                    return {"result": "rejected", "task_id": task_id, "error": "task_id_conflict"}
                return get(task_id)
            task = _Task(fingerprint, {"result": "executing", "task_id": task_id})
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
                task.result = {**result, "task_id": task_id}
                _write(task_id, task.fingerprint, task.result)
                task.ready.set()
        threading.Thread(target=run, name=f"lamp-task-{task_id[:16]}", daemon=True).start()
    task.ready.wait(max(0.0, min(sync_window_s, 25.0)))
    return get(task_id) or {"result": "error", "task_id": task_id}
