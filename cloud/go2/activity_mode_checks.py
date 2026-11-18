"""Activity ownership is released by measured stops, never RPC acceptance."""
import asyncio
import importlib

import pytest

from cloud.go2.user_handoff_checks import handoff, stationary
from cloud.space.execution import ExecutionContext


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_activity_end_releases_only_after_fresh_stop(handoff, monkeypatch, confirmed):
    h = handoff
    h.lease.acquire(ExecutionContext('act_previous', 'visitor', False))
    monkeypatch.setattr(h.service, '_activity_mode_owner', 'act_previous')
    monkeypatch.setattr(h.service, '_configure_vision_consumers',
                        lambda mode: h.robot.runtime.commit('test_mode', autonomy_mode=mode))
    gateway = importlib.import_module('cloud.peers.gateway')
    monkeypatch.setattr(gateway, 'cancel_pending', lambda **_kwargs: None)
    task = asyncio.create_task(h.service.set_activity_mode('manual', 'act_previous'))
    await asyncio.sleep(.02)
    if confirmed:
        stationary(h.robot)
    result = await task
    assert result['ok'] is True
    assert result['physical_stop'] == ('confirmed' if confirmed else 'unknown')
    assert (h.lease.snapshot() is None) is confirmed
    # No second uncorrelated Sport RPC turns accepted into completion proof.
    h.robot._conn.datachannel.pub_sub.publish_request_new.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_activity_end_cannot_stop_new_control(handoff, monkeypatch):
    h = handoff
    h.lease.acquire(ExecutionContext('new-user', 'visitor', False))
    monkeypatch.setattr(h.service, '_activity_mode_owner', 'act_new')
    result = await h.service.set_activity_mode('manual', 'act_old')
    assert result['reason'] == 'not_activity_owned'
    h.robot._conn.datachannel.pub_sub.publish_without_callback.assert_not_called()
    assert h.lease.snapshot().owner_session_id == 'new-user'
