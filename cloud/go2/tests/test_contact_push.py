"""Contact-only avoidance switching and the real Navigator, without hardware."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud.go2.tests.test_relative_move import movement


@pytest.fixture
def contact(movement, monkeypatch):
    from cloud.go2.navigation import contact_push as module, navigator
    robot = movement.robot
    robot._conn = object()
    enabled, writes = [True], []
    async def read():
        return {'result': 'ok', 'enabled': enabled[0]}
    async def write(value, *, connection):
        assert connection is robot._conn
        writes.append(value)
        enabled[0] = value
        return {'result': 'ok', 'enabled': value}
    robot.read_avoidance_switch = AsyncMock(side_effect=read)
    robot.set_avoidance_switch = AsyncMock(side_effect=write)
    monkeypatch.setattr(module, 'go2', robot)
    monkeypatch.setattr(module, 'control_lease', navigator.control_lease)
    navigator.control_lease.acquire(movement.context)
    return SimpleNamespace(module=module, movement=movement, enabled=enabled, writes=writes)


@pytest.mark.asyncio
async def test_only_contact_forward_ignores_obstacles_and_restores_switch(contact):
    m, module = contact.movement, contact.module
    grid = m.robot.occupancy_grid
    grid.grid[:] = True
    grid.is_free = lambda *_: False
    grid.is_body_clear = lambda *_: False
    async def move():
        assert contact.enabled == [False]
        return await m.nav.move_relative('forward', distance_m=.4, speed=.25, context=m.context)
    result = await module.execute_contact_push(m.context, move)
    assert result['result'] == 'ok', result
    assert result['distance_m'] >= .35 and result['stop']['result'] == 'ok'
    assert contact.writes == [False, True] and contact.enabled == [True]
    assert result['avoidance']['restored'] is True
    assert grid.grid.all(), 'Contact must not rewrite the shared obstacle map'
    assert all(vy == yaw == 0 for _, vy, yaw in m.commands)
    assert m.commands[-1] == (0, 0, 0)
    # The following ordinary move has no contact privilege.
    ordinary = await m.nav.move_relative('forward', distance_m=.2, context=m.context)
    assert ordinary['result'] == 'blocked'
    assert ordinary['reason'] == 'no_known_free_path'


@pytest.mark.asyncio
async def test_contact_does_not_enable_a_previously_disabled_switch(contact):
    contact.enabled[0] = False
    operation = AsyncMock(return_value={'result': 'ok', 'stop': {'result': 'ok'}})
    result = await contact.module.execute_contact_push(contact.movement.context, operation)
    assert result['result'] == 'ok' and contact.writes == []
    assert contact.enabled == [False] and result['avoidance']['restored']


@pytest.mark.asyncio
async def test_unknown_original_switch_never_disables_or_moves(contact):
    contact.movement.robot.read_avoidance_switch.side_effect = None
    contact.movement.robot.read_avoidance_switch.return_value = {'result': 'unknown'}
    operation = AsyncMock()
    result = await contact.module.execute_contact_push(contact.movement.context, operation)
    assert result['result'] == 'blocked' and result['reason'] == 'avoidance_switch_unknown'
    operation.assert_not_awaited()
    contact.movement.robot.set_avoidance_switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_contact_still_stops_when_localization_packets_stop(contact):
    m = contact.movement
    m.fault['stale_after'] = 3
    result = await contact.module.execute_contact_push(m.context, lambda:
        m.nav.move_relative('forward', distance_m=1., speed=.25, context=m.context))
    assert result['result'] != 'ok'
    assert result['navigation']['reason'] == 'localization_unavailable'
    assert m.commands[-1] == (0, 0, 0)
    assert contact.writes == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize('during_set', [False, True])
async def test_cancellation_waits_for_stop_and_restoration(contact, during_set):
    m, module = contact.movement, contact.module
    entered, order = asyncio.Event(), []
    async def write(value, *, connection):
        contact.writes.append(value)
        contact.enabled[0] = value
        order.append(('switch', value))
        if not value and during_set:
            entered.set()
            await asyncio.Event().wait()
    m.robot.set_avoidance_switch.side_effect = write
    async def stop(**_):
        order.append(('stop', True))
        return {'result': 'ok'}
    m.nav.stop_and_confirm = AsyncMock(side_effect=stop)
    async def operation():
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(module.execute_contact_push(m.context, operation))
    await asyncio.wait_for(entered.wait(), 1.)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert contact.enabled == [True]
    assert order == [('switch', False), ('stop', True), ('switch', True)]
    assert module._contact.get() is None


@pytest.mark.asyncio
async def test_unknown_disable_result_never_moves_and_still_restores(contact):
    m = contact.movement
    async def write(value, *, connection):
        contact.writes.append(value)
        contact.enabled[0] = value
        if not value:
            raise TimeoutError('set_response_lost')
    m.robot.set_avoidance_switch.side_effect = write
    operation = AsyncMock()
    result = await contact.module.execute_contact_push(m.context, operation)
    operation.assert_not_awaited()
    assert result['result'] == 'blocked' and contact.writes == [False, True]
    assert contact.enabled == [True]


@pytest.mark.asyncio
async def test_restore_failure_preserves_motion_and_keeps_lease_occupied(contact):
    m, module = contact.movement, contact.module
    async def write(value, *, connection):
        if value:
            raise TimeoutError('restore_response_lost')
    m.robot.set_avoidance_switch.side_effect = write
    result = await module.execute_contact_push(m.context, AsyncMock(return_value={
        'result': 'ok', 'distance_m': .4, 'stop': {'result': 'ok'}}))
    assert result['result'] == 'accepted'
    assert result['reason'] == 'avoidance_restore_unconfirmed' and result['distance_m'] == .4
    owner = module.control_lease.snapshot()
    assert owner and not module.control_lease.allows(owner.owner_session_id, owner.generation)


@pytest.mark.asyncio
async def test_old_contact_cannot_control_a_replacement_connection(contact):
    m, module = contact.movement, contact.module
    async def operation():
        m.robot._conn = object()
        return {'result': 'cancelled', 'stop': {'result': 'accepted'}}
    result = await module.execute_contact_push(m.context, operation)
    assert result['result'] == 'accepted'
    assert contact.writes == [False]  # Never send the old restoration to a new connection.
    assert result['avoidance']['restore_error'] == 'avoidance_connection_changed'
    assert not m.commands


@pytest.mark.asyncio
async def test_contact_rejects_sideways_motion_and_still_restores(contact):
    m = contact.movement
    result = await contact.module.execute_contact_push(m.context, lambda:
        m.nav.move_relative('left', distance_m=.4, context=m.context))
    assert result['result'] == 'blocked'
    assert result['reason'] == 'contact_push_requires_original_forward_motion'
    assert not any(any(command) for command in m.commands)
    assert contact.writes == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_native_switch_requires_ack_and_readback(confirmed):
    from cloud.go2.move_control_checks import robot_at_stand
    from unitree_webrtc_connect.constants import OBSTACLES_AVOID_API, RTC_TOPIC
    robot, rpc, _ = robot_at_stand()
    async def respond(topic, options):
        assert topic == RTC_TOPIC['OBSTACLES_AVOID']
        if options['api_id'] == OBSTACLES_AVOID_API['SWITCH_SET']:
            assert options['parameter'] == {'enable': False}
            return {'data': {'header': {'status': {'code': 0}}}}
        return {'data': {'header': {'status': {'code': 0}},
                         'data': {'enable': not confirmed}}}
    rpc.side_effect = respond
    if confirmed:
        result = await robot.set_avoidance_switch(False, connection=robot._conn)
        assert result['enabled'] is False
    else:
        with pytest.raises(ValueError, match='unconfirmed'):
            await robot.set_avoidance_switch(False, connection=robot._conn)


@pytest.mark.asyncio
async def test_next_motion_recovers_pending_avoidance_before_cached_gait():
    from cloud.go2.move_control_checks import robot_at_stand
    from unitree_webrtc_connect.constants import OBSTACLES_AVOID_API
    robot, rpc, _ = robot_at_stand()
    robot._avoidance_restore_expected = True
    rpc.side_effect = [
        {'data': {'header': {'status': {'code': 0}}}},
        {'data': {'header': {'status': {'code': 0}}, 'data': {'enable': True}}},
    ]
    await robot.prepare_motion()
    assert robot._avoidance_restore_expected is None
    assert [call.args[1]['api_id'] for call in rpc.await_args_list] == [
        OBSTACLES_AVOID_API['SWITCH_SET'], OBSTACLES_AVOID_API['SWITCH_GET']]
