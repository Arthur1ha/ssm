"""Durable per-run JSON journal; uncertain side effects are never replayed."""

from __future__ import annotations

import time
from dataclasses import asdict, fields
from pathlib import Path

from cloud.agent_runtime.json_files import locked_directory, read_record, record_path, write_record
from cloud.agent_runtime.loop import Run


class RunStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, run_id: str) -> Path:
        return record_path(self.path, run_id)

    @staticmethod
    def _data(run: Run, updated_at: float | None = None) -> dict:
        return {"schema_version": 1, **asdict(run), "updated_at": updated_at or time.time()}

    @staticmethod
    def _run(data: dict) -> Run:
        allowed = {field.name for field in fields(Run)}
        run = Run(**{name: value for name, value in data.items() if name in allowed})
        if any(step.get("dispatch_status") != "observed" for step in run.steps):
            run.status = "unknown"
            run.response = "进程中断时有动作状态未确认；请先查询原任务，不能自动重发。"
        return run

    def create(self, run: Run) -> bool:
        """Reserve an ID before a model call or any side effect."""
        with locked_directory(self.path):
            return write_record(self._path(run.run_id), self._data(run), exclusive=True)

    def save(self, run: Run, _step: dict | None = None) -> None:
        with locked_directory(self.path):
            path = self._path(run.run_id)
            old = read_record(path)
            if old is not None:
                if old["session_id"] != run.session_id or old["goal"] != run.goal:
                    raise ValueError("run_identity_conflict")
            write_record(path, self._data(run))

    def get(self, run_id: str, session_id: str) -> Run | None:
        with locked_directory(self.path):
            data = read_record(self._path(run_id))
        if data is None or data.get("session_id") != session_id:
            return None
        return self._run(data)

    def claim_resume(self, run_id: str, session_id: str) -> Run | None:
        """Only one process may claim a paused run."""
        with locked_directory(self.path):
            path = self._path(run_id)
            data = read_record(path)
            if data is None or data.get("session_id") != session_id or data.get("status") != "needs_clarification":
                return None
            data.update(status="executing", question="", updated_at=time.time())
            write_record(path, data)
            return self._run(data)

    def latest_paused(self, session_id: str) -> Run | None:
        with locked_directory(self.path):
            latest: dict | None = None
            for path in self.path.glob("*.json"):
                data = read_record(path)
                if data and data.get("session_id") == session_id and data.get("status") == "needs_clarification":
                    if latest is None or data.get("updated_at", 0) > latest.get("updated_at", 0):
                        latest = data
        return self._run(latest) if latest else None
