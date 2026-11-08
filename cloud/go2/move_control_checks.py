"""Movement selection activates the firmware; all packets are simulated."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud.go2.connection.webrtc import Go2Connection
from unitree_webrtc_connect.constants import RTC_TOPIC


def robot_at_stand():
    robot = Go2Connection()
    robot.is_connected = True
    robot.runtime.body('standing', confirmation='observed')
    robot._sport_state_observed_at = time.monotonic()
    publish = AsyncMock(return_value={'data': {'header': {'status': {'code': 0}}}})
    sticks = MagicMock()
    robot._conn = SimpleNamespace(datachannel=SimpleNamespace(pub_sub=SimpleNamespace(
        publish_request_new=publish, publish_without_callback=sticks)))
    return robot, publish, sticks


@pytest.mark.asyncio
@pytest.mark.parametrize('condition',['offline','lying','sitting','stale'])
async def test_activation_keeps_connection_and_posture_guards(condition):
    robot, publish, sticks = robot_at_stand()
    if condition == 'offline':
        robot.is_connected = False
    elif condition == 'stale':
        robot._sport_state_observed_at = time.monotonic() - 10
    else:
        robot.runtime.body(condition, confirmation='observed')
    with pytest.raises((ValueError,RuntimeError)):
        await robot.send_command('EnterMoveControl')
    publish.assert_not_awaited()
    sticks.assert_not_called()


@pytest.mark.asyncio
async def test_entry_activates_firmware_before_enabling_both_joysticks():
    robot, publish, sticks = robot_at_stand()
    result = await robot.send_command('EnterMoveControl')
    assert result['result'] == 'accepted'
    assert robot.runtime.data['control_mode'] == 'remote'
    assert [call.args[1] for call in publish.await_args_list] == [
        {'api_id':1002}, {'api_id':1027,'parameter':{'data':True}}]
    assert all(call.args[0] == RTC_TOPIC['SPORT_MOD'] for call in publish.await_args_list)
    sticks.assert_not_called()  # selecting movement never starts driving
    robot.move_velocity(.2, -.1, .3)
    sticks.assert_called_once_with(RTC_TOPIC['WIRELESS_CONTROLLER'],
                                   data={'lx':.1,'ly':.2,'rx':-.3,'ry':0})
    robot.move_velocity(0, 0, 0)
    assert sticks.call_args.kwargs['data'] == {'lx':0,'ly':0,'rx':0,'ry':0}
    assert robot.runtime.data['control_mode'] == 'remote'  # releasing is not exiting


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_at',[0,1])
@pytest.mark.parametrize('response',[{}, {'data':{'header':{'status':{'code':7}}}}])
async def test_unconfirmed_or_rejected_activation_never_enables_control(failure_at,response):
    robot, publish, sticks = robot_at_stand()
    good = {'data':{'header':{'status':{'code':0}}}}
    publish.side_effect = [good] * failure_at + [response]
    if response:
        result = await robot.send_command('EnterMoveControl')
        assert result['result'] == 'error'
    else:
        with pytest.raises(ValueError, match='尚未确认'):
            await robot.send_command('EnterMoveControl')
    assert publish.await_count == failure_at + 1
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
    assert all(not any(call.kwargs['data'].values()) for call in sticks.call_args_list)


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
    assert order[0] == ('stick',{'lx':0,'ly':0,'rx':0,'ry':0})
    assert order[1][0] == 'rpc'
    assert robot.runtime.data['control_mode'] == 'normal'
