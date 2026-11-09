"""Velocity/odom feedback tests. No network, models or physical robot."""
import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud.go2.actions import move_tool_schema, validate_move_goal
from cloud.go2.execution import ExecutionContext, use_context
from cloud.go2.tools.executor import validate_params


@pytest.fixture
def movement(monkeypatch):
    from cloud.go2.navigation import navigator as nav_module
    from cloud.go2.tools import tools, executor
    from cloud.go2.control import ControlLease
    nav = nav_module.Navigator()
    pose = {'x': .013, 'y': -.017, 'heading': .7, 'session_id': 'map'}
    robot = SimpleNamespace(is_connected=True, _odom_session_id='map', odom=pose,
        odom_received_at=time.monotonic(), voxel_received_at=time.monotonic(),
        odom_frame_id='odom', voxel_frame_id='odom')
    commands = []
    fault = {'stall': False, 'block_after': None, 'stale_after': None, 'sideways_only': False}
    start = pose.copy()
    grid = SimpleNamespace(frame_id='odom', session_id='map', observed_at=time.monotonic(),
        has_free_space_evidence=True, resolution=.05,
        odom_to_grid=lambda x, y: (round(x/.05), round(y/.05)),
        grid_to_odom=lambda x, y: (x*.05, y*.05), is_free=lambda *args: True,
        is_turn_clear=lambda *args: True)
    def body_clear(x, y, heading):
        return fault['block_after'] is None or math.hypot(x-start['x'], y-start['y']) < fault['block_after']
    grid.is_body_clear = body_clear
    robot.occupancy_grid = grid
    def move(vx, vy, yaw, **kwargs):
        commands.append((vx, vy, yaw))
        if not fault['stall']:
            h = pose['heading']
            wx, wy = vx*math.cos(h)-vy*math.sin(h), vx*math.sin(h)+vy*math.cos(h)
            if fault['sideways_only'] and (vx or vy):
                wx, wy = -.3*math.sin(start['heading']), .3*math.cos(start['heading'])
            pose['x'] += wx*.1
            pose['y'] += wy*.1
            pose['heading'] = math.atan2(math.sin(h+yaw*.1), math.cos(h+yaw*.1))
        return {'result': 'accepted'}
    robot.move_velocity = move
    real_sleep = asyncio.sleep
    async def tick(_delay):
        await real_sleep(.001)
        stamp = time.monotonic()
        stale = fault['stale_after'] is not None and len(commands) > fault['stale_after']
        robot.odom_received_at = stamp-2 if stale else stamp
        robot.voxel_received_at = grid.observed_at = stamp
    monkeypatch.setattr(nav_module.asyncio, 'sleep', tick)
    monkeypatch.setattr(nav_module, 'go2', robot)
    monkeypatch.setattr(nav_module, 'runtime_for', lambda _: None)
    monkeypatch.setattr(nav_module, 'control_lease', ControlLease())
    monkeypatch.setattr(nav_module, 'navigator', nav)
    monkeypatch.setattr(nav_module, 'MOTION_PROGRESS_TIMEOUT_S', .015)
    monkeypatch.setattr(nav_module, 'TELEMETRY_RECOVERY_S', .005)
    monkeypatch.setattr(nav_module, 'STOP_CONFIRMATION_S', .01)
    monkeypatch.setattr(executor, 'execute_action', AsyncMock(return_value={'result': 'accepted'}))
    monkeypatch.setattr(tools, 'go2', robot)
    context = ExecutionContext('move-test', 'visitor', False, expires_at=time.time()+60)
    return SimpleNamespace(nav=nav, robot=robot, commands=commands, fault=fault,
        start=start, context=context, gateway=executor.execute_action)


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['forward', 'backward', 'left', 'right'])
async def test_distance_uses_projected_odometry_and_streams_until_stopped(movement, direction):
    from cloud.go2.tools.tools import go2_move
    with use_context(movement.context):
        result = await go2_move(direction, distance_m=1.)
    assert result['result'] == 'ok', result
    assert .95 <= result['distance_m'] <= 1.05
    assert result['stop']['result'] == 'ok'
    assert result['confirmation'] == 'odometry_and_stop'
    assert len([cmd for cmd in movement.commands if any(cmd)]) > 10
    assert movement.commands[-1] == (0, 0, 0)
    assert [call.args[0] for call in movement.gateway.await_args_list] == ['EnterMoveControl', 'Move']
    assert '指令已发送' not in result['message']
    assert '停稳' in result['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['turn_left', 'turn_right'])
async def test_turn_uses_measured_angle_without_translation(movement, direction):
    from cloud.go2.tools.tools import go2_move
    with use_context(movement.context):
        result = await go2_move(direction, angle_deg=90.)
    assert result['result'] == 'ok', result
    assert abs(result['angle_deg'] - 90) <= 3
    assert all(vx == vy == 0 for vx, vy, yaw in movement.commands)


@pytest.mark.asyncio
async def test_obstruction_reports_partial_distance_and_stops(movement):
    from cloud.go2.tools.tools import go2_move
    movement.fault['block_after'] = .4
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.)
    assert result['result'] == 'blocked'
    assert 0 < result['distance_m'] < .4
    assert result['requested_distance_m'] == 1.
    assert result['stop']['result'] == 'ok'
    assert '未完成' in result['message']


@pytest.mark.asyncio
async def test_no_motion_never_counts_time_as_distance(movement):
    from cloud.go2.tools.tools import go2_move
    movement.fault['stall'] = True
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.)
    assert result['result'] == 'blocked'
    assert result['reason'] == 'motion_not_observed'
    assert result['distance_m'] == 0
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_sideways_motion_does_not_count_as_forward_distance(movement, monkeypatch):
    from cloud.go2.tools.tools import go2_move
    from cloud.go2.navigation import navigator as nav_module
    monkeypatch.setattr(nav_module, 'NAV_TIMEOUT', .05)
    movement.fault['sideways_only'] = True
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.)
    assert result['result'] == 'blocked'
    assert abs(result['distance_m']) < .01


@pytest.mark.asyncio
async def test_training_rejection_never_starts_velocity_stream(movement):
    from cloud.go2.tools.tools import go2_move
    movement.gateway.side_effect = [{'result': 'accepted'},
        {'result': 'blocked', 'error': 'training_blocked'}]
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.)
    assert result['reason'] == 'training_blocked'
    assert result['distance_m'] == 0
    assert not any(any(cmd) for cmd in movement.commands)


@pytest.mark.asyncio
async def test_changed_session_never_measures_from_another_origin(movement):
    from cloud.go2.tools.tools import go2_move
    original = movement.robot.move_velocity
    def move(*args, **kwargs):
        result = original(*args, **kwargs)
        if any(args):
            movement.robot._odom_session_id = 'new-map'
            movement.robot.odom.update(x=10., y=10., session_id='new-map')
        return result
    movement.robot.move_velocity = move
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.)
    assert result['result'] != 'ok'
    assert result['distance_m'] is None or result['distance_m'] < .1


@pytest.mark.asyncio
async def test_stop_without_fresh_odometry_never_reports_success(movement):
    from cloud.go2.tools.tools import go2_move
    movement.fault['stale_after'] = 5
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.)
    assert result['result'] == 'accepted'
    assert result['reason'] == 'stop_unconfirmed'
    assert result['distance_m'] < 1.
    assert '尚未确认' in result['message']


@pytest.mark.asyncio
async def test_cancellation_stops_before_propagating(movement):
    from cloud.go2.tools.tools import go2_move
    async def moving():
        with use_context(movement.context):
            return await go2_move('forward', distance_m=5.)
    task = asyncio.create_task(moving())
    while not any(any(cmd) for cmd in movement.commands):
        await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.parametrize('params', [
    {'direction': 'forward', 'duration': 3.3},
    {'direction': 'forward'},
    {'direction': 'forward', 'distance_m': 1, 'angle_deg': 90},
    {'direction': 'turn_left', 'distance_m': 1},
    {'direction': 'turn_right', 'angle_deg': float('nan')},
    {'direction': 'forward', 'distance_m': -1},
])
def test_invalid_or_old_move_requests_are_rejected(params):
    with pytest.raises(ValueError):
        validate_params(move_tool_schema(), params)
        validate_move_goal(params)
