"""Default gait regressions using simulated transports only."""

import asyncio

import pytest

from cloud.go2.move_control_checks import robot_at_stand
from cloud.go2.tests.test_relative_move import movement


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [0, -1])
async def test_coordinate_navigation_prepares_gait_before_streaming(movement, code):
    from cloud.go2.navigation import navigator as module

    robot, rpc, packets = robot_at_stand()
    robot._classic_walk_connection = None
    rpc.return_value = {'data': {'header': {'status': {'code': code}}}}
    movement.robot.prepare_motion = robot.prepare_motion
    original = movement.robot.move_velocity
    sent_at = []
    clock = module.time.monotonic

    def move(vx, vy, yaw, **kwargs):
        if vx or vy or yaw:
            assert robot._classic_walk_connection is robot._conn
            sent_at.append(clock())
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = move
    pose = movement.robot.odom
    pose.update(y=0., heading=0.)
    result = await movement.nav.go_to_coordinate(pose['x']+.6, 0., speed_limit=.25,
                                                arrival_tolerance_m=.05)
    assert [call.args[1]['api_id'] for call in rpc.await_args_list] == [2049]
    packets.assert_not_called()  # Gait preparation never commands a speed.
    assert movement.commands[-1] == (0, 0, 0)
    if code == 0:
        assert result['status'] == 'succeeded', result
        assert len(sent_at) > 10
        assert max(b-a for a, b in zip(sent_at, sent_at[1:])) < .3
        assert max(vx for vx, _, _ in movement.commands) == pytest.approx(.25)
        active = [i for i, command in enumerate(movement.commands) if any(command)]
        assert all(any(command) for command in movement.commands[active[0]:active[-1]+1])
        await robot.prepare_motion()
        assert rpc.await_count == 1  # Reuse the confirmed gait in this connection.
    else:
        assert result['status'] == 'blocked', result
        assert not sent_at


@pytest.mark.asyncio
async def test_navigation_rechecks_session_after_gait_preparation(movement):
    async def prepare():
        movement.robot._odom_session_id = 'replacement'
    movement.robot.prepare_motion = prepare
    result = await movement.nav.go_to_coordinate(.4, .3)
    assert result['status'] == 'failed' and result['reason'] == 'map_mismatch'
    assert not any(any(command) for command in movement.commands)


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
