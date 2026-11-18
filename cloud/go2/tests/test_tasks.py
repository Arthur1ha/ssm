import asyncio
import time

import pytest

from cloud.go2.tasks import ChatTaskRegistry


@pytest.mark.asyncio
async def test_running_task_can_be_queried_and_retried_without_duplicate_execution():
    registry = ChatTaskRegistry()
    release = asyncio.Event()
    calls = 0

    async def run():
        nonlocal calls
        calls += 1
        await release.wait()
        return {"result": "ok", "reply": "done"}

    record = registry.start("task-a", "session-a", "same", time.time() + 5, run)
    assert (await registry.wait(record, 0))["result"] == "executing"
    assert registry.start("task-a", "session-a", "same", time.time() + 5, run) is record
    assert registry.get("task-a", "session-b") is None
    with pytest.raises(ValueError, match="task_id_conflict"):
        registry.start("task-a", "session-a", "different", time.time() + 5, run)
    release.set()
    assert (await registry.wait(record, 1))["result"] == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_cancel_requires_confirmed_stop():
    registry = ChatTaskRegistry()
    async def run():
        await asyncio.Event().wait()
        return {"result": "ok"}

    first = registry.start("task-a", "session-a", "a", time.time() + 5, run)
    result = await registry.cancel(first, lambda: asyncio.sleep(0, result={"result": "accepted"}))
    assert result["result"] == "accepted"
    assert result["error"] == "stop_unconfirmed"

    second = registry.start("task-b", "session-a", "b", time.time() + 5, run)
    result = await registry.cancel(second, lambda: asyncio.sleep(0, result={"result": "ok"}))
    assert result["result"] == "cancelled"


@pytest.mark.asyncio
async def test_deadline_keeps_unconfirmed_stop_visible():
    registry = ChatTaskRegistry()

    async def run():
        await asyncio.Event().wait()

    record = registry.start(
        "task-deadline", "session-a", "same", time.time() + 0.01, run,
        on_timeout=lambda: asyncio.sleep(0, result={"result": "accepted"}),
    )
    result = await registry.wait(record, 1)
    assert result["result"] == "accepted"
    assert result["error"] == "stop_unconfirmed"
    assert result["task_id"] == "task-deadline"


@pytest.mark.asyncio
async def test_accepted_device_action_can_still_be_cancelled():
    registry = ChatTaskRegistry()
    record = registry.start(
        "accepted-action", "session-a", "same", time.time() + 5,
        lambda: asyncio.sleep(0, result={"result": "accepted"}),
    )
    assert (await registry.wait(record, 1))["result"] == "accepted"
    result = await registry.cancel(record, lambda: asyncio.sleep(0, result={"result": "ok"}))
    assert result["result"] == "cancelled"
