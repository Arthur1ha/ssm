"""Session owned Go2 chat tasks with idempotent submission and visible results."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class ChatTask:
    task_id: str
    session_id: str
    fingerprint: str
    deadline: float
    trace_id: str = ""
    created_at: float = field(default_factory=time.time)
    result: dict | None = None
    task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        if self.result is None:
            return {"task_id": self.task_id, "trace_id": self.trace_id,
                    "result": "executing", "deadline": self.deadline}
        return {**self.result, "task_id": self.task_id, "trace_id": self.trace_id}


class ChatTaskRegistry:
    def __init__(self, retention_s: float = 300.0) -> None:
        self._records: dict[str, ChatTask] = {}
        self._retention_s = retention_s

    def _prune(self) -> None:
        now = time.time()
        for task_id, record in tuple(self._records.items()):
            if record.result is not None and now - record.created_at > self._retention_s:
                self._records.pop(task_id, None)

    def get(self, task_id: str, session_id: str) -> ChatTask | None:
        self._prune()
        record = self._records.get(task_id)
        return record if record is not None and record.session_id == session_id else None

    def start(
        self,
        task_id: str,
        session_id: str,
        fingerprint: str,
        deadline: float,
        run: Callable[[], Awaitable[dict]],
        on_timeout: Callable[[], Awaitable[dict]] | None = None,
        trace_id: str = "",
    ) -> ChatTask:
        self._prune()
        existing = self._records.get(task_id)
        if existing is not None:
            if existing.session_id != session_id or existing.fingerprint != fingerprint:
                raise ValueError("task_id_conflict")
            return existing
        record = ChatTask(task_id, session_id, fingerprint, deadline, trace_id)
        self._records[task_id] = record

        async def execute() -> None:
            try:
                remaining = max(0.0, deadline - time.time())
                result = await asyncio.wait_for(run(), timeout=remaining)
                record.result = result if isinstance(result, dict) else {"result": "error", "error": "invalid_response"}
            except asyncio.TimeoutError:
                if on_timeout is None:
                    record.result = {"result": "timeout", "error": "server_deadline_exceeded"}
                else:
                    try:
                        stopped = await asyncio.wait_for(on_timeout(), timeout=10)
                    except Exception:
                        stopped = {"result": "accepted", "error": "stop_unconfirmed"}
                    if stopped.get("result") == "ok":
                        record.result = {"result": "timeout", "error": "server_deadline_exceeded", "stop": stopped}
                    else:
                        record.result = {"result": "accepted", "error": "stop_unconfirmed", "stop": stopped}
            except asyncio.CancelledError:
                # Cancellation of the coroutine alone does not prove the robot
                # has stopped. The explicit stop callback may upgrade this.
                record.result = {"result": "accepted", "error": "stop_unconfirmed"}
                raise
            except Exception:
                record.result = {"result": "error", "error": "task_failed"}

        record.task = asyncio.create_task(execute(), name=f"go2-chat-{task_id}")
        return record

    async def wait(self, record: ChatTask, window_s: float) -> dict:
        if record.task is not None and record.result is None:
            try:
                await asyncio.wait_for(asyncio.shield(record.task), timeout=max(0.0, window_s))
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                if record.task.cancelled():
                    pass
                else:
                    raise
        return record.snapshot()

    async def cancel(self, record: ChatTask, stop: Callable[[], Awaitable[dict]]) -> dict:
        if record.result is not None and record.result.get("result") not in {"accepted", "executing"}:
            return record.snapshot()
        stop_task = asyncio.create_task(stop())
        def finish_stop(_done: asyncio.Task) -> None:
            if not stop_task.done() or (record.task is not None and not record.task.done()):
                return
            try:
                stopped_result = stop_task.result()
            except BaseException:
                stopped_result = {"result": "accepted", "error": "stop_unconfirmed"}
            if stopped_result.get("result") == "ok":
                record.result = {"result": "cancelled", "stop": stopped_result}
            else:
                record.result = {"result": "accepted", "error": "stop_unconfirmed", "stop": stopped_result}
        stop_task.add_done_callback(finish_stop)
        if record.task is not None:
            record.task.add_done_callback(finish_stop)
            record.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(record.task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        try:
            stopped = await asyncio.wait_for(asyncio.shield(stop_task), timeout=10)
        except Exception:
            stopped = {"result": "accepted", "error": "stop_unconfirmed"}
        finish_stop(stop_task)
        if record.result is None:
            record.result = {"result": "accepted", "error": "stop_unconfirmed", "stop": stopped}
        return record.snapshot()


chat_tasks = ChatTaskRegistry()
