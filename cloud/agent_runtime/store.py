"""Durable run journal. A pending side effect after restart remains unknown.

This journal complements device task registries; their task/request IDs are kept
in step results and must be queried through the device registry before resuming.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cloud.agent_runtime.loop import Run


class RunStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, goal TEXT NOT NULL,
                deadline REAL NOT NULL, status TEXT NOT NULL, response TEXT NOT NULL,
                question TEXT NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL, step_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY (run_id, step_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id))""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def save(self, run: Run, _step: dict | None = None) -> None:
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
                response=excluded.response, question=excluded.question,
                updated_at=excluded.updated_at""",
                (run.run_id, run.session_id, run.goal, run.deadline,
                 run.status, run.response, run.question, time.time()))
            for ordinal, step in enumerate(run.steps):
                db.execute("""INSERT INTO steps VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id,step_id) DO UPDATE SET data=excluded.data""",
                    (run.run_id, step["step_id"], ordinal,
                     json.dumps(step, ensure_ascii=False, default=str)))

    def create(self, run: Run) -> bool:
        """Atomically reserve a run ID before any model or side effect starts."""
        with self._lock, self._connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (run.run_id, run.session_id, run.goal, run.deadline,
                                 run.status, run.response, run.question, time.time()))
            return cursor.rowcount == 1

    def claim_resume(self, run_id: str, session_id: str) -> Run | None:
        """Only one continuation may consume a paused run, including across processes."""
        with self._lock, self._connect() as db:
            cursor = db.execute("""UPDATE runs SET status='executing', question='', updated_at=?
                WHERE run_id=? AND session_id=? AND status='needs_clarification'""",
                (time.time(), run_id, session_id))
            if cursor.rowcount != 1:
                return None
        return self.get(run_id, session_id)

    def get(self, run_id: str, session_id: str) -> Run | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=? AND session_id=?",
                             (run_id, session_id)).fetchone()
            if row is None:
                return None
            steps = [json.loads(data) for (data,) in db.execute(
                "SELECT data FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,))]
        run = Run(row[0], row[1], row[2], row[3], steps, row[4], row[5], row[6])
        if any(step.get("dispatch_status") != "observed" for step in steps):
            run.status = "unknown"
            run.response = "进程中断时有动作状态未确认；请先查询原任务，不能自动重发。"
        return run

    def latest_paused(self, session_id: str) -> Run | None:
        with self._lock, self._connect() as db:
            row = db.execute("""SELECT run_id FROM runs WHERE session_id=? AND status='needs_clarification'
                ORDER BY updated_at DESC LIMIT 1""", (session_id,)).fetchone()
        return self.get(row[0], session_id) if row else None
