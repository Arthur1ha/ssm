"""Default gait regressions using simulated transports only."""

import asyncio

import pytest

from cloud.go2.move_control_checks import robot_at_stand


@pytest.mark.asyncio
async def test_tasks_and_joystick_select_classic_once_per_connection():
    robot, rpc, packets = robot_at_stand()
    robot._classic_walk_connection = None
    await robot.send_command('EnterMoveControl')
    assert [c.args[1]['api_id'] for c in rpc.await_args_list] == [1003, 2049]
    assert rpc.await_args.args[1]['parameter'] == {'data': True}
    packets.assert_not_called()
    try:
        robot.manual_velocity(.2, .1, .3, can_continue=lambda: True)
        await asyncio.sleep(.04)
        robot.move_velocity(0, 0, 0)
        await robot.send_command('Move', {'x': .2})
        assert sum(c.args[1]['api_id'] == 2049 for c in rpc.await_args_list) == 1
        robot.move_velocity(0, 0, 0)
        replacement, replacement_rpc, _ = robot_at_stand()
        robot._conn = replacement._conn
        await robot.send_command('EnterMoveControl')
        assert [c.args[1]['api_id'] for c in replacement_rpc.await_args_list] == [1003, 2049]
    finally:
        robot.move_velocity(0, 0, 0)


@pytest.mark.asyncio
async def test_direct_move_selects_gait_before_sending_velocity():
    robot, rpc, _ = robot_at_stand()
    robot._classic_walk_connection = None
    await robot.send_command('Move', {'x': .2})
    assert [c.args[1]['api_id'] for c in rpc.await_args_list] == [2049, 1008]


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [None, {}, {'data': 'invalid'},
    {'data': {'header': {'status': {'code': -1}}}},
    {'data': {'header': {'status': {'code': False}}}}])
async def test_unconfirmed_gait_blocks_move_and_keeps_stop_available(reply):
    robot, rpc, packets = robot_at_stand()
    robot._classic_walk_connection = None
    rpc.return_value = reply
    with pytest.raises(ValueError, match='步态尚未确认'):
        await robot.send_command('Move', {'x': .2})
    assert robot.is_connected
    assert robot._classic_walk_connection is None
    assert [c.args[1]['api_id'] for c in rpc.await_args_list] == [2049]
    packets.assert_not_called()
    robot.move_velocity(0, 0, 0)
    assert packets.call_args.kwargs['data']['header']['identity']['api_id'] == 1003
