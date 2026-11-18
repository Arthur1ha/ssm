"""Server-owned latest user target, shared by API and orchestration processes.

This is cancellation provenance, not an agent loop or a device-state source.
Only authenticated external entry points advance it; child tools inherit it.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from hashlib import sha256

from cloud.storage.json_files import locked_directory, read_record, write_record


class GoalSuperseded(asyncio.CancelledError):
    """A newer authenticated user target replaced this Graph run."""


class GoalRegistry:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.path = self.directory / "current.json"

    def current(self) -> dict:
        # The initial positive generation also binds autonomous work received
        # before the first user command; zero means deliberately unbound only.
        return read_record(self.path) or {"revision": 1}

    def advance(self, session_id: str, run_id: str = "") -> int:
        if not session_id:
            raise ValueError("trusted user session required")
        with locked_directory(self.directory):
            current = self.current()
            identity = sha256(f"{session_id}\0{run_id}".encode()).hexdigest()
            request = self.directory / "requests" / (identity + ".json")
            prior = read_record(request) if run_id else None
            if prior:
                if current["revision"] != prior["revision"]:
                    raise GoalSuperseded("request_already_superseded")
                return prior["revision"]
            revision = max(int(current["revision"]) + 1, int(time.time() * 1000))
            if run_id:
                write_record(request, {"schema_version": 1, "revision": revision})
            write_record(self.path, {"schema_version": 1, "revision": revision,
                                    "session_id": session_id, "run_id": run_id})
        return revision

    def is_current(self, revision: int) -> bool:
        # Unbound maintenance/autonomous callers retain their existing guards.
        return revision == 0 or self.current()["revision"] == revision

    def check(self, revision: int) -> None:
        if not self.is_current(revision):
            raise GoalSuperseded("newer_user_goal")


def get_goal_registry() -> GoalRegistry:
    # No import-time disk writes, and tests can use an isolated directory.
    return GoalRegistry(os.getenv("SSM_USER_GOALS_DIR", str(
        Path(__file__).parent / "data" / "user_goals")))


async def run_for_goal(awaitable, revision: int):
    """Cancel the existing LangGraph task even while its model/HTTP awaits.

    Synchronous adapters already use daemon threads via invoke_blocking. Their
    late receipts stay attached to the original effect identity; cancelling
    this await never waits for or replays that physical dispatch.
    """
    if not revision:
        return await awaitable
    registry = get_goal_registry()
    task = asyncio.create_task(awaitable)

    async def watch():
        while registry.is_current(revision):
            await asyncio.sleep(.05)
        raise GoalSuperseded("newer_user_goal")

    watcher = asyncio.create_task(watch())
    try:
        registry.check(revision)
        done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            await watcher
        try:
            result = await task
        except BaseException:
            # LangGraph distinguishes node-raised CancelledError from an
            # externally cancelled task and may wrap it as NodeCancelledError.
            # The server-owned version remains the authoritative reason.
            registry.check(revision)
            raise
        registry.check(revision)
        return result
    finally:
        for child in (task, watcher):
            if not child.done():
                child.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)
