"""In-process Go2 movement lease and session-owned task lifecycle."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass

from cloud.go2.execution import ExecutionContext


class LeaseConflict(RuntimeError):
    def __init__(self, owner_session_id: str, expires_at: float) -> None:
        super().__init__("Go2 is currently controlled by another visitor")
        self.owner_session_id = owner_session_id
        self.expires_at = expires_at


@dataclass(frozen=True)
class LeaseSnapshot:
    owner_session_id: str
    owner_actor: str
    expires_at: float


class ControlLease:
    def __init__(self, ttl_s: float | None = None) -> None:
        self._ttl_override = ttl_s
        self._owner_session_id = ""
        self._owner_actor = ""
        self._expires_at = 0.0
        self._lock = threading.RLock()

    @property
    def ttl_s(self) -> float:
        raw = self._ttl_override if self._ttl_override is not None else os.getenv("GO2_VISITOR_LEASE_TTL_S", "30")
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            return 30.0

    def _expire_unlocked(self, now: float) -> None:
        if self._owner_session_id and self._expires_at <= now:
            self._owner_session_id = ""
            self._owner_actor = ""
            self._expires_at = 0.0

    def acquire(self, context: ExecutionContext, now: float | None = None) -> LeaseSnapshot:
        now = time.time() if now is None else now
        with self._lock:
            self._expire_unlocked(now)
            same_owner = self._owner_session_id == context.session_id
            admin_preempts = context.actor == "admin"
            if self._owner_session_id and not same_owner and not admin_preempts:
                raise LeaseConflict(self._owner_session_id, self._expires_at)
            self._owner_session_id = context.session_id
            self._owner_actor = context.actor
            self._expires_at = now + self.ttl_s
            return self._snapshot_unlocked()

    def heartbeat(self, context: ExecutionContext, now: float | None = None) -> LeaseSnapshot:
        now = time.time() if now is None else now
        with self._lock:
            self._expire_unlocked(now)
            if self._owner_session_id != context.session_id:
                raise LeaseConflict(self._owner_session_id, self._expires_at)
            self._expires_at = now + self.ttl_s
            return self._snapshot_unlocked()

    def release(self, session_id: str) -> bool:
        with self._lock:
            self._expire_unlocked(time.time())
            if self._owner_session_id != session_id:
                return False
            self._owner_session_id = ""
            self._owner_actor = ""
            self._expires_at = 0.0
            return True

    def snapshot(self) -> LeaseSnapshot | None:
        with self._lock:
            self._expire_unlocked(time.time())
            return self._snapshot_unlocked() if self._owner_session_id else None

    def _snapshot_unlocked(self) -> LeaseSnapshot:
        return LeaseSnapshot(self._owner_session_id, self._owner_actor, self._expires_at)


class SessionTasks:
    def __init__(self) -> None:
        self._tasks: dict[str, set[asyncio.Task]] = {}

    def track(self, session_id: str, task: asyncio.Task) -> asyncio.Task:
        tasks = self._tasks.setdefault(session_id, set())
        tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            owned = self._tasks.get(session_id)
            if owned is not None:
                owned.discard(done)
                if not owned:
                    self._tasks.pop(session_id, None)

        task.add_done_callback(_done)
        return task

    def cancel(self, session_id: str) -> int:
        tasks = tuple(self._tasks.pop(session_id, ()))
        for task in tasks:
            if not task.done():
                task.cancel()
        return len(tasks)


control_lease = ControlLease()
session_tasks = SessionTasks()

