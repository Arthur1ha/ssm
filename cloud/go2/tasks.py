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


def command_identity(context, request_id: str) -> tuple[str, str, str, str]:
    """Namespace client retries by the server-verified origin and session."""
    import json
    import re
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id):
        raise ValueError("invalid_request_id")
    scope = json.dumps([context.space_id, context.origin_kind, context.session_id], separators=(",", ":"))
    return "go2", scope, "direct-command", request_id


def _completion_identity(identity, phase="completion"):
    return (*identity[:3], identity[3] + ":" + phase)


def command_result(record: dict, request_id: str) -> dict:
    """Project the dispatch receipt without inventing physical completion."""
    result = dict(record.get("result") or {
        "result": "accepted", "error": "command_result_unconfirmed",
        "confirmation_scope": "request", "replayed": False,
    })
    result["command_request_id"] = request_id
    return result


def observe_command_results(robot) -> None:
    """Capture terminal device facts before a later action replaces last_action.

    This is an index into the existing effect ledger, not a second task queue.
    The dispatch receipt stays immutable; completion is a separate immutable
    observation under the same request identity.
    """
    import logging
    from cloud.go2.state import runtime_for
    runtime = runtime_for(robot)
    if runtime is None:
        return
    snapshot = runtime.snapshot()
    records = getattr(robot, "_command_result_records", {})
    last = snapshot.get("last_action") or {}
    key = (snapshot["epoch"], last.get("request_id"))
    entry = records.get(key)
    phase = "timeout" if last.get("result") == "timeout" else "completion"
    if (entry is not None and last.get("result") in {"ok", "error", "rejected", "cancelled", "timeout"}
            and phase not in entry[3]):
        ledger, identity, dispatched, observed = entry
        result = {**dispatched, "result": last["result"], "last_action": last,
                  "confirmation": last.get("confirmation"),
                  "confirmation_scope": "completion" if last["result"] == "ok" else "request"}
        result.pop("error", None)
        if last.get("error"):
            result["error"] = last["error"]
        try:
            completion_id = _completion_identity(identity, phase)
            ledger.reserve(*completion_id, "go2_completion", {
                "device_request_id": key[1], "device_epoch": key[0]})
            ledger.mark_result(*completion_id, result)
        except Exception:
            # Persistence failure cannot stop telemetry processing. Retain the
            # index so a later state notification or query can retry the write.
            logging.exception("[Go2/Command] failed to persist physical receipt")
        else:
            observed.add(phase)
            from cloud.observability import emit_agent_event
            import json
            _space, origin, session = json.loads(identity[1])
            emit_agent_event("device.result", trace_id=result.get("trace_id"), agent_id="go2",
                             session_id=session, origin_kind=origin, task_id=identity[3],
                             request_id=key[1], transport="webrtc", result=result,
                             status=result["result"], confirmation_scope=result["confirmation_scope"])
            if phase == "completion":
                records.pop(key, None)
    # An old connection can never complete a new connection's action. Its
    # durable reservation remains queryable as unconfirmed, without replay.
    for old_key in tuple(records):
        if old_key[0] != snapshot["epoch"]:
            records.pop(old_key, None)


def track_command_result(robot, ledger, identity, result) -> None:
    if result.get("result") not in {"accepted", "executing", "timeout"}:
        return
    physical_id = result.get("request_id") or (result.get("pending_action") or {}).get("request_id")
    epoch = result.get("device_epoch")
    if not physical_id or not epoch:
        return
    records = getattr(robot, "_command_result_records", None)
    if records is None:
        records = robot._command_result_records = {}
    records[(epoch, physical_id)] = (ledger, identity, dict(result), set())
    robot._command_result_callback = lambda: observe_command_results(robot)
    observe_command_results(robot)


def read_command_result(context, request_id: str, *, robot=None) -> dict | None:
    from cloud.agents.common.effects import FileEffectLedger
    ledger = FileEffectLedger()
    identity = command_identity(context, request_id)
    record = ledger.read(*identity)
    if record is None:
        return None
    if robot is not None:
        observe_command_results(robot)
    for phase in ("completion", "timeout"):
        completed = ledger.read(*_completion_identity(identity, phase))
        if completed is not None and completed.get("result") is not None:
            return command_result(completed, request_id)
    return command_result(record, request_id)
