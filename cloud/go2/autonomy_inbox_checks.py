"""Peer delivery and autonomous handoff safety contracts."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud.go2.mode import free_explore
from cloud.go2.navigation.navigator import navigator


@pytest.mark.asyncio
async def test_peer_event_is_bounded_deduplicated_and_rejected_after_stop(monkeypatch):
    drive = free_explore.Drive()
    monkeypatch.setattr(free_explore.go2, "is_connected", True)
    drive._loop = asyncio.get_running_loop()
    drive._task = asyncio.create_task(asyncio.sleep(60))
    drive._wake = asyncio.Event()
    message = {"message_id": "msg-one", "target": "go2", "sender": "lamp",
               "content": "你好", "deadline": time.time() + 30}
    messenger = SimpleNamespace(send_result=lambda *args, **kwargs: None)
    assert drive.submit_peer_message(message, messenger)
    assert drive.submit_peer_message(message, messenger)
    await asyncio.sleep(0)
    assert len(drive._events) == 1
    assert drive._wake.is_set()
    drive.stop()
    assert not drive.submit_peer_message(message, messenger)


@pytest.mark.asyncio
async def test_navigation_handoff_requires_confirmed_stop(monkeypatch):
    drive = free_explore.Drive()
    drive.owner_session_id = "autonomous-owner"
    drive._context = SimpleNamespace(space_id="main")
    drive._events.append({"kind": "peer_message", "event_id": "msg-one"})
    drive._interrupt_for_event = AsyncMock(return_value=True)
    monkeypatch.setattr(free_explore.go2, "is_connected", True)
    monkeypatch.setattr(free_explore.go2, "send_command", AsyncMock(return_value={"result": "timeout"}))
    stop_calls = []
    monkeypatch.setattr(navigator, "stop", lambda owner_session_id=None: stop_calls.append(owner_session_id) or True)
    async def slow_motion(*_args, **_kwargs):
        await asyncio.sleep(60)
        return {"result": "ok"}
    drive._execute_motion = AsyncMock(side_effect=slow_motion)
    result = await drive._navigate(coordinate=(1.0, 0.0))
    assert stop_calls == ["autonomous-owner"]
    assert result["result"] == "unknown"
    assert result["reason"] == "stop_unconfirmed"
    assert drive.user_interrupt is True
