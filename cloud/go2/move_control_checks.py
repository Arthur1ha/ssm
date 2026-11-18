"""Movement selection activates the firmware; all packets are simulated."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2.connection.webrtc import Go2Connection
from unitree_webrtc_connect.constants import RTC_TOPIC, DATA_CHANNEL_TYPE


def robot_at_stand():
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body('standing', confirmation='observed')
    robot._sport_state_observed_at = time.monotonic()
    publish = AsyncMock(return_value={'data': {'header': {'status': {'code': 0}}}})
    sticks = MagicMock()
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=publish, publish_without_callback=sticks)))
    robot._classic_walk_connection = robot._conn  # Existing connection already prepared.
    robot._native_motion_connection = robot._conn
    robot._native_motion_mode = 'mcf'
    robot.read_motion_mode = AsyncMock(return_value={'result': 'ok', 'name': 'mcf'})
    return robot, publish, sticks


@pytest.mark.asyncio
@pytest.mark.parametrize('condition',['offline','lying','sitting'])
async def test_activation_keeps_connection_and_posture_guards(condition):
    robot, publish, sticks = robot_at_stand()
    if condition == 'offline':
        robot.is_connected = False
    else:
        robot.runtime.body(condition, confirmation='observed')
    with pytest.raises((ValueError,RuntimeError)):
        await robot.send_command('EnterMoveControl')
    publish.assert_not_awaited()
    sticks.assert_not_called()


@pytest.mark.asyncio
async def test_entry_prepares_sport_channel_without_entering_balance_stand():
    robot, publish, sticks = robot_at_stand()
    result = await robot.send_command('EnterMoveControl')
    assert result['result'] == 'accepted'
    assert robot.runtime.data['control_mode'] == 'remote'
    assert publish.await_count == 1
    prepare = publish.await_args.args[1]
    assert prepare['api_id'] == 1003
    assert type(prepare['id']) is int and prepare['id'] > 0
    assert all(call.args[0] == RTC_TOPIC['SPORT_MOD'] for call in publish.await_args_list)
    sticks.assert_not_called()  # selecting movement never starts driving
    robot.move_velocity(.2, -.1, .3)
    assert sticks.call_args.args == (RTC_TOPIC['SPORT_MOD'],)
    packet = sticks.call_args.kwargs['data']
    assert packet['header']['identity']['api_id'] == 1008
    assert json.loads(packet['parameter']) == {'x':.2,'y':-.1,'z':.3}
    assert sticks.call_args.kwargs['msg_type'] == DATA_CHANNEL_TYPE['MSG']
    assert packet['header']['policy'] == {'priority': 0, 'noreply': True}
    assert packet['binary'] == []
    robot.move_velocity(0, 0, 0)
    assert sticks.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
    assert sticks.call_args.kwargs['data']['parameter'] == ''
    assert sticks.call_args.kwargs['msg_type'] == DATA_CHANNEL_TYPE['REQUEST']
    assert 'policy' not in sticks.call_args.kwargs['data']['header']
    ids = [call.kwargs['data']['header']['identity']['id'] for call in sticks.call_args_list]
    assert len(set(ids)) == len(ids)
    assert all(call.args[0] != RTC_TOPIC['WIRELESS_CONTROLLER'] for call in sticks.call_args_list)
    assert robot.runtime.data['control_mode'] == 'remote'  # releasing is not exiting


@pytest.mark.asyncio
@pytest.mark.parametrize('response',[{}, {'data':{'header':{'status':{'code':7}}}}])
async def test_unconfirmed_or_rejected_activation_never_enables_control(response):
    robot, publish, sticks = robot_at_stand()
    publish.return_value = response
    if response:
        result = await robot.send_command('EnterMoveControl')
        assert result['result'] == 'error'
    else:
        with pytest.raises(ValueError, match='尚未确认'):
            await robot.send_command('EnterMoveControl')
    assert publish.await_count == 1
    assert robot.runtime.data['control_mode'] == 'normal'
    sticks.assert_not_called()


@pytest.mark.asyncio
async def test_zero_during_activation_cannot_later_enable_control():
    robot, publish, sticks = robot_at_stand()
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        entered.set()
        await release.wait()
        return {'data':{'header':{'status':{'code':0}}}}
    publish.side_effect = delayed
    task = asyncio.create_task(robot.send_command('EnterMoveControl'))
    await entered.wait()
    robot.stop_velocity()
    release.set()
    assert (await task)['result'] == 'cancelled'
    assert publish.await_count == 1
    assert robot.runtime.data['control_mode'] == 'normal'
    assert all(call.args[0] == RTC_TOPIC['SPORT_MOD']
               and call.kwargs['data']['header']['identity']['api_id'] == 1003
               and call.kwargs['data']['parameter'] == '' for call in sticks.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('command',['Sit','StandDown','Hello','StopMove'])
async def test_leaving_control_zeros_the_stick_before_body_rpc(command):
    robot, publish, sticks = robot_at_stand()
    await robot.send_command('EnterMoveControl')
    robot.move_velocity(.2, 0, .3)
    order = []
    sticks.side_effect = lambda *args,**kwargs: order.append(('stick',kwargs['data']))
    async def body_rpc(*args):
        order.append(('rpc',args[1]))
        return {'data':{'header':{'status':{'code':0}}}}
    publish.side_effect = body_rpc
    result = await robot.send_command(command, dispatch_only=command != 'StopMove')
    assert result['result'] == 'accepted'
    assert order[0][0] == 'stick'
    assert order[0][1]['header']['identity']['api_id'] == 1003
    assert order[0][1]['parameter'] == ''
    assert order[1][0] == 'rpc'
    assert robot.runtime.data['control_mode'] == 'normal'


@pytest.mark.asyncio
async def test_missing_stick_heartbeat_sends_native_stopmove():
    robot, publish, packets = robot_at_stand()
    await robot.send_command('EnterMoveControl')
    robot.move_velocity(.2, 0, .3, deadman_s=.01)
    await asyncio.sleep(.03)
    assert [call.kwargs['data']['header']['identity']['api_id'] for call in packets.call_args_list] == [1008,1003]
    assert robot._velocity_command_active is False


@pytest.mark.asyncio
async def test_admitted_move_and_navigation_share_no_reply_packets_without_move_rpc():
    robot, rpc, packets = robot_at_stand()

    async def reject_move_rpc(topic, options):
        assert options['api_id'] != 1008, 'Move must not use an RPC path'
        return {'data': {'header': {'status': {'code': 0}}}}

    rpc.side_effect = reject_move_rpc
    result = await robot.send_command('Move', {'x': .2})
    assert result['result'] == 'accepted'
    assert result['confirmation'] == 'transport'
    assert result['request_status_code'] is None
    assert robot._last_velocity_packet['source'] == 'command'
    robot.move_velocity(.2, 0, 0, source='navigation', deadman_s=.3)
    assert robot._last_velocity_packet['source'] == 'navigation'
    rpc.assert_not_awaited()
    assert packets.call_count == 2
    for call in packets.call_args_list:
        assert call.kwargs['msg_type'] == DATA_CHANNEL_TYPE['MSG']
        assert call.kwargs['data']['header']['identity']['api_id'] == 1008
        assert call.kwargs['data']['header']['policy']['noreply'] is True
        assert json.loads(call.kwargs['data']['parameter']) == {'x': .2, 'y': 0., 'z': 0.}
    robot.move_velocity(0, 0, 0)


@pytest.mark.asyncio
async def test_admitted_move_has_same_deadman_when_no_navigation_refresh_follows(monkeypatch):
    from cloud.go2.connection import webrtc
    robot, rpc, packets = robot_at_stand()
    monkeypatch.setattr(webrtc, 'COMMAND_TTL_S', .02)
    await robot.send_command('Move', {'x': .2})
    await asyncio.sleep(.05)
    assert [call.kwargs['data']['header']['identity']['api_id']
            for call in packets.call_args_list] == [1008, 1003]
    assert robot._last_velocity_packet['source'] == 'watchdog'
    assert not robot._velocity_command_active
    rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_admitted_move_retires_existing_joystick_ticks():
    robot, rpc, packets = robot_at_stand()
    robot._move_control_active = True
    robot.manual_velocity(.3, 0, 0, can_continue=lambda: True)
    await asyncio.sleep(.04)
    await robot.send_command('Move', {'y': .2})
    count = packets.call_count
    assert robot._manual_velocity_handle is None
    assert robot._last_velocity_packet['source'] == 'command'
    await asyncio.sleep(.06)
    assert packets.call_count == count, 'An old joystick tick overwrote the admitted task'
    assert json.loads(packets.call_args.kwargs['data']['parameter']) == {'x': 0., 'y': .2, 'z': 0.}
    robot.move_velocity(0, 0, 0)
