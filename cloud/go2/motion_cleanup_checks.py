"""Movement cleanup uses real leases and body facts; no live device I/O."""
import asyncio
from dataclasses import replace

import pytest

from cloud.go2.state import Go2State
from cloud.go2.tests.test_relative_move import movement


@pytest.fixture
def owned_motion(movement, monkeypatch):
    from cloud.go2.navigation import navigator as module

    h = movement
    runtime = Go2State()
    runtime.connection('online')
    runtime.body('standing', confirmation='observed')
    h.runtime = runtime
    h.lease = module.control_lease
    h.robot.runtime = runtime
    h.robot.body_state = 'standing'
    h.robot.pending_action = None
    h.robot.body_ready_reason = lambda: None
    h.robot._move_control_active = False
    h.robot._motion_epoch = 0
    h.released = []
    monkeypatch.setattr(module, 'runtime_for', lambda _: runtime)
    original_move = h.robot.move_velocity

    def move(vx, vy, yaw, **kwargs):
        h.robot._motion_epoch += 1
        return original_move(vx, vy, yaw, **kwargs)

    def stop():
        h.released.append(runtime.snapshot())
        move(0, 0, 0)
        h.robot._move_control_active = False
        runtime.commit('remote_exit', control_mode='normal')

    async def gateway(command, *args, context):
        h.lease.acquire(context)
        h.robot._motion_epoch += 1
        if command == 'EnterMoveControl':
            h.robot._move_control_active = True
            runtime.commit('remote', control_mode='remote')
        return {'result': 'accepted'}

    h.gateway.side_effect = gateway
    h.robot.move_velocity = move
    h.robot.stop_velocity = stop
    return h


@pytest.mark.parametrize('direction,goal', [('forward', {'distance_m': .3}),
                                         ('turn_left', {'angle_deg': 30.})])
async def test_completed_move_leaves_its_control_after_observed_standing(owned_motion, direction, goal):
    h = owned_motion
    result = await h.nav.move_relative(direction, context=h.context, **goal)
    assert result['result'] == 'ok', result
    assert result['stop']['result'] == 'ok'
    assert len(h.released) == 1
    assert h.released[0]['body_state'] == 'standing'
    assert h.runtime.snapshot()['control_mode'] == 'normal'
    assert h.commands[-1] == (0, 0, 0)


async def test_existing_remote_control_is_not_released_by_relative_move(owned_motion):
    h = owned_motion
    h.robot._move_control_active = True
    h.runtime.commit('user_remote', control_mode='remote')
    result = await h.nav.move_relative('forward', distance_m=.3, context=h.context)
    assert result['result'] == 'ok'
    assert h.released == []
    assert h.runtime.snapshot()['control_mode'] == 'remote'


@pytest.mark.parametrize('takeover', ['same_session', 'other_session', 'reconnect', 'velocity'])
async def test_takeover_during_stop_cannot_be_cleared_by_old_movement(owned_motion, monkeypatch, takeover):
    from cloud.go2.navigation import navigator as module

    h = owned_motion
    original_confirm = h.nav._confirm_stopped
    old_sleep = asyncio.sleep

    async def confirm(*args, **kwargs):
        if kwargs.get('_move_control'):
            async def changed(delay):
                await old_sleep(delay)
                if takeover == 'reconnect':
                    h.robot._odom_session_id = 'new-map'
                elif takeover == 'velocity':
                    h.robot._motion_epoch += 1
                else:
                    context = (h.context if takeover == 'same_session' else
                               replace(h.context, session_id='new-user', actor='admin'))
                    h.lease.acquire(context)
            monkeypatch.setattr(module.asyncio, 'sleep', changed)
        return await original_confirm(*args, **kwargs)

    monkeypatch.setattr(h.nav, '_confirm_stopped', confirm)
    result = await h.nav.move_relative('forward', distance_m=.3, context=h.context)
    assert result['result'] == 'accepted'
    assert result['stop']['diagnostics']['reason'] == 'control_owner_changed'
    assert h.released == []
    assert h.runtime.snapshot()['control_mode'] == 'remote'


async def test_odom_stop_waits_for_delayed_standing_feedback(owned_motion, monkeypatch):
    h = owned_motion
    original_confirm = h.nav._confirm_stopped
    seen = []

    async def confirm(*args, **kwargs):
        if kwargs.get('_move_control'):
            h.runtime.body(None, confirmation='unknown')
            async def body_feedback():
                for _ in range(8):
                    await asyncio.sleep(.1)
                seen.append((h.runtime.snapshot()['control_mode'],
                             h.nav.last_stop_diagnostics.copy()))
                h.runtime.body('standing', confirmation='observed')
            update = asyncio.create_task(body_feedback())
            try:
                return await original_confirm(*args, **kwargs)
            finally:
                await update
        return await original_confirm(*args, **kwargs)

    monkeypatch.setattr(h.nav, '_confirm_stopped', confirm)
    result = await h.nav.move_relative('forward', distance_m=.3, context=h.context)
    assert result['result'] == 'ok'
    assert seen[0][0] == 'remote'
    assert seen[0][1]['samples'] == 3
    assert seen[0][1]['reason'] == 'standing_unconfirmed'
    assert h.runtime.snapshot()['body_state'] == 'standing'
    assert h.runtime.snapshot()['control_mode'] == 'normal'


async def test_missing_standing_feedback_does_not_fake_success_or_exit_control(owned_motion, monkeypatch):
    h = owned_motion
    original_confirm = h.nav._confirm_stopped

    async def confirm(*args, **kwargs):
        if kwargs.get('_move_control'):
            h.runtime.body(None, confirmation='unknown')
        return await original_confirm(*args, **kwargs)

    monkeypatch.setattr(h.nav, '_confirm_stopped', confirm)
    result = await h.nav.move_relative('forward', distance_m=.3, context=h.context)
    assert result['result'] == 'accepted'
    assert result['stop']['diagnostics']['reason'] == 'standing_unconfirmed'
    assert h.released == []
    assert h.runtime.snapshot()['body_state'] is None

