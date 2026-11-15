"""Peer delivery and autonomous handoff safety contracts."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud.go2.mode import free_explore
from cloud.go2.navigation.navigator import navigator
from cloud.peers import games


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


@pytest.mark.asyncio
async def test_replayed_game_invite_does_not_execute_again(monkeypatch, tmp_path):
    from cloud.agents.common import effects

    drive = free_explore.Drive()
    drive._activity_id = lambda: "act_test"
    monkeypatch.setattr(free_explore, "runtime_for", lambda _robot: None)
    # This checks replay reservation after the separately tested live gates.
    monkeypatch.setattr("cloud.peers.games.base.game_invitation_ready", lambda *_args: True)
    monkeypatch.setattr("cloud.peers.inbound.message_activity_allows", lambda _message: True)
    monkeypatch.setattr(games, "activity_allows", lambda *_args: True)
    monkeypatch.setattr(games, "game_is_playable", lambda *_args: True)
    ledger_type = effects.FileEffectLedger
    monkeypatch.setattr(effects, "FileEffectLedger", lambda: ledger_type(tmp_path))
    executions = []

    async def execute(_drive, _event, _task_id):
        executions.append("started")

    monkeypatch.setattr(games, "get_game", lambda _game_id:
                        SimpleNamespace(game_id="color_hunt", execute=execute))
    outcomes = []
    messenger = SimpleNamespace(
        send_progress=lambda *_args, **_kwargs: None,
        send_result=lambda *args, **kwargs: outcomes.append((args, kwargs)))
    event = {"kind": "peer_message", "event_id": "msg_replay",
             "message_id": "msg_replay", "activity_id": "act_test",
             "interaction_kind": "game_invite", "game_id": "color_hunt",
             "game_preaccepted": True, "original_invitation_id": "msg_original",
             "deadline": time.time() + 30,
             "message": {"sender": "esp32_desk_led", "target": "go2"},
             "messenger": messenger, "decision": {"choice": "accept"}}

    await drive._handle_event(event, None, None, None)
    await drive._handle_event(event, None, None, None)
    assert executions == ["started"]
    assert outcomes[-1][0][1] == "unknown"
    assert outcomes[-1][1]["reason"] == "prior_game_execution_unconfirmed"
