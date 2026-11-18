"""Velocity/odom feedback tests. No network, models or physical robot."""
import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import numpy as np

from cloud.go2.actions import move_tool_schema, validate_move_goal
from cloud.space.execution import ExecutionContext, use_context
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
    fault = {'stall': False, 'block_after': None, 'stale_after': None, 'sideways_only': False,
             'minimum_linear_speed': 0.}
    start = pose.copy()
    clock = [time.monotonic()]
    monkeypatch.setattr(nav_module, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time))
    grid = SimpleNamespace(frame_id='odom', session_id='map', observed_at=time.monotonic(),
        has_free_space_evidence=True, resolution=.05, grid=np.zeros((40, 40), dtype=bool),
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
            if math.hypot(vx, vy) < fault['minimum_linear_speed']:
                wx = wy = 0.
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
        clock[0] += _delay
        stamp = clock[0]
        stale = fault['stale_after'] is not None and len(commands) > fault['stale_after']
        if not stale:
            robot.odom_received_at = stamp
        robot.voxel_received_at = grid.observed_at = stamp
    monkeypatch.setattr(nav_module.asyncio, 'sleep', tick)
    monkeypatch.setattr(nav_module, 'go2', robot)
    monkeypatch.setattr(nav_module, 'runtime_for', lambda _: None)
    monkeypatch.setattr(nav_module, 'control_lease', ControlLease())
    monkeypatch.setattr(nav_module, 'navigator', nav)
    monkeypatch.setattr(nav_module, 'MOTION_PROGRESS_TIMEOUT_S', 1.)
    monkeypatch.setattr(nav_module, 'TELEMETRY_RECOVERY_S', .3)
    monkeypatch.setattr(nav_module, 'STOP_CONFIRMATION_S', 3.)
    monkeypatch.setattr(executor, 'execute_action', AsyncMock(return_value={'result': 'accepted'}))
    monkeypatch.setattr(tools, 'go2', robot)
    context = ExecutionContext('move-test', 'visitor', False, expires_at=time.time()+60)
    return SimpleNamespace(nav=nav, robot=robot, commands=commands, fault=fault,
        start=start, context=context, gateway=executor.execute_action)


@pytest.mark.asyncio
async def test_forward_arrival_accepts_small_heading_drift_without_extra_turn(movement):
    """20:34 reached 1.506/1.5m but 3.3 degrees of drift failed arrival."""
    from cloud.go2.tools.tools import go2_move

    original = movement.robot.move_velocity
    moved = False

    def brake_drift(vx, vy, yaw, **kwargs):
        nonlocal moved
        if vx:
            moved = True
        elif moved and not vy and not yaw:
            movement.robot.odom['heading'] = movement.start['heading'] + math.radians(3.3)
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = brake_drift
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=1.5)
    assert result['result'] == 'ok', result
    assert result['stop']['result'] == 'ok'
    assert result['distance_m'] == pytest.approx(1.5, abs=.05)
    assert not any(vy or yaw for _, vy, yaw in movement.commands)


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
@pytest.mark.parametrize('recover', [True, False])
async def test_pause_between_preflight_and_stream_waits_for_body_feedback(movement, monkeypatch, recover):
    from cloud.go2.connection.fsm import CommandBlocked
    from cloud.go2.navigation import navigator as nav_module
    from cloud.go2.tools.tools import go2_move
    original_move, original_tick = movement.robot.move_velocity, nav_module.asyncio.sleep
    feedback = {'reason': None, 'active': 0, 'paused': False, 'ticks': 0}
    monkeypatch.setattr(nav_module, 'TELEMETRY_RECOVERY_S', .3)
    movement.robot.body_ready_reason = lambda: feedback['reason']

    def move(vx, vy, yaw, **kwargs):
        if any((vx, vy, yaw)):
            feedback['active'] += 1
            if feedback['active'] == 2:
                # The planner/deadman sends StopMove after preflight; fresh
                # body telemetry has not arrived when this stream update runs.
                original_move(0, 0, 0)
                feedback.update(reason='等待有效设备反馈', paused=True)
                raise CommandBlocked(feedback['reason'])
            assert feedback['reason'] is None, 'Motion resumed without real feedback'
        return original_move(vx, vy, yaw, **kwargs)

    async def tick(delay):
        await original_tick(delay)
        if feedback['reason']:
            feedback['ticks'] += 1
            if recover and feedback['ticks'] >= 2:
                feedback['reason'] = None

    movement.robot.move_velocity = move
    monkeypatch.setattr(nav_module.asyncio, 'sleep', tick)
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=.4)
    assert feedback['paused'] and feedback['ticks'] >= 2
    assert result['reason'] != 'control_error'
    if recover:
        assert result['result'] == 'ok', result
        assert result['distance_m'] >= .35
    else:
        assert result['result'] == 'blocked', result
        assert result['reason'] == 'posture_not_confirmed'
        assert feedback['active'] == 2
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_real_body_rejection_preserves_reason_instead_of_claiming_disconnect(movement):
    from cloud.go2.connection.fsm import CommandBlocked
    from cloud.go2.tools.tools import go2_move
    original_move = movement.robot.move_velocity

    def move(vx, vy, yaw, **kwargs):
        if any((vx, vy, yaw)):
            raise CommandBlocked('body_action_unconfirmed')
        return original_move(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = move
    with use_context(movement.context):
        result = await go2_move('forward', distance_m=.4)
    assert result['result'] == 'blocked'
    assert result['reason'] == 'body_action_unconfirmed'
    assert '连接异常' not in result['message']
    assert not any(any(command) for command in movement.commands)


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
async def test_explicit_cargo_approach_uses_measured_forward_motion_without_station_precision(movement, monkeypatch, tmp_path):
    """Approach follows the existing measured forward-motion contract, without side trips."""
    from cloud.go2.navigation import cargo_push
    from cloud.go2.navigation import navigator as nav_module
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    pose = movement.robot.odom
    # Translate the recorded odom geometry into the fake map's positive bounds.
    pose.update(x=1.024342, y=3.017677, heading=.06040686965152552)
    movement.robot.occupancy_grid.grid = np.zeros((160, 160), dtype=bool)
    cargo = {'x': 4.382444471060111, 'y': 2.0321784052445094,
             'source': 'visual_distance_estimate', 'precision': 'approximate'}
    sample = {'pose': dict(pose), 'odom_session_id': 'map',
        'cargo_location': cargo, 'cargo_bbox_1000': [672, 612, 758, 662],
        'destination_location': {'x': 4.809291794668575, 'y': 2.683830000982215},
        'near_destination': False, 'advance_distance_m': .6}
    sample['cargo_nearby'] = False
    monkeypatch.setattr(cargo_push, 'navigator', movement.nav)
    monkeypatch.setattr(cargo_push, '_authorize', nav_module.control_lease.acquire)
    monkeypatch.setattr(cargo_push, 'observe_cargo', AsyncMock(return_value=sample))
    from cloud.go2.tools.tools import go2_move
    async def execute(name, params, **_kwargs):
        assert name == 'go2_move'
        return await go2_move(**params)
    monkeypatch.setattr('cloud.go2.tools.executor.execute_tool', execute)
    async def authorized(operation, _context, *, on_result=None):
        result = await operation
        if on_result:
            on_result(result)
        return result
    monkeypatch.setattr(cargo_push, '_await_authorized', authorized)
    async def stop(context, *, bounded):
        return await movement.nav.stop_and_confirm(owner_session_id=context.session_id)
    monkeypatch.setattr(cargo_push, '_stop_approach', stop)
    contact = AsyncMock(return_value={'result': 'ok', 'stop': {'result': 'ok'}})
    monkeypatch.setattr(cargo_push, 'execute_contact_push', contact)
    movement.fault['minimum_linear_speed'] = .20
    with use_context(movement.context):
        result = await cargo_push.push_cargo('近处右侧的深色扁盒子', '左后方蓝色点位', operation='approach')
    assert result['result'] == 'ok', (result.get('reason'), result.get('movement'))
    assert result['progress'] == 'approached' and result['directional_push_attempted'] is False
    assert result['stop']['result'] == 'ok' and result['final'] is False
    contact.assert_not_awaited()
    assert any(vx >= .2 for vx, _, _ in movement.commands)
    assert all(vy == 0 for _, vy, _ in movement.commands)
    assert result['movement']['distance_m'] >= .55
    assert all(sum(abs(v) > 1e-9 for v in command) <= 1 for command in movement.commands)
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_replay_delivery_positioning_does_not_stall_at_last_18cm(movement):
    """18:42 real trace stalled at .14 m/s, .18 m short of the waypoint."""
    # Translate the recorded y coordinates into this fake map's positive grid.
    movement.robot.odom.update(x=.54, y=2.192, heading=0.)
    movement.robot.occupancy_grid.grid = np.zeros((160, 160), dtype=bool)
    movement.fault['minimum_linear_speed'] = .20
    result = await movement.nav.go_to_coordinate(1.482, 1.183, holonomic=True,
        speed_limit=.25, minimum_command_speed=.25, arrival_tolerance_m=.1,
        face_target=(2.2, 2.2))
    assert result['status'] == 'succeeded', result
    pose = movement.robot.odom
    assert math.hypot(pose['x'] - 1.482, pose['y'] - 1.183) < .1
    assert max(math.hypot(vx, vy) for vx, vy, _ in movement.commands) == pytest.approx(.25)
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('final_heading', [0., math.pi/2])
async def test_cargo_positioning_uses_body_translation_before_one_final_turn(movement, final_heading):
    pose = movement.robot.odom
    pose.update(x=.5, y=.5, heading=0.)
    result = await movement.nav.go_to_coordinate(.9, 1.1, strafe=True,
        coarse_arrival=True, heading=final_heading, speed_limit=.25, minimum_command_speed=.25)
    assert result['status'] == 'succeeded', result
    assert any(vy > 0 for _, vy, _ in movement.commands)
    assert all(sum(abs(v) > 1e-9 for v in command) <= 1 for command in movement.commands)
    yaw_steps = [i for i, command in enumerate(movement.commands) if command[2]]
    if final_heading:
        assert yaw_steps
        assert all(vx == vy == 0 for vx, vy, _ in movement.commands[yaw_steps[0]:])
    else:
        assert not yaw_steps
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_cargo_sideways_positioning_keeps_live_obstacle_checks(movement):
    pose = movement.robot.odom
    pose.update(x=.5, y=.5, heading=0.)
    movement.robot.occupancy_grid.is_body_clear = lambda x, y, h: y < .65
    result = await movement.nav.go_to_coordinate(.5, 1.1, strafe=True,
        coarse_arrival=True, heading=0., speed_limit=.25, minimum_command_speed=.25)
    assert result['status'] == 'blocked', result
    assert any(vy > 0 for _, vy, _ in movement.commands)
    assert pose['y'] < .65
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_visual_cargo_right_turn_uses_working_angular_default(movement, monkeypatch, tmp_path):
    """Replay a 16-degree cargo alignment with ineffective low yaw commands."""
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.go2.navigation import cargo_push
    from cloud.go2.navigation import target_approach
    from cloud.go2.control import ControlLease
    from cloud.go2.tools import executor
    from cloud.go2.tools.tools import go2_move

    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    monkeypatch.setattr(cargo_push, 'navigator', movement.nav)
    lease = ControlLease()
    lease.acquire(movement.context)
    monkeypatch.setattr(target_approach, 'control_lease', lease)
    actual_move = movement.robot.move_velocity

    def gait(vx, vy, yaw, **kwargs):
        return actual_move(vx, vy, yaw if abs(yaw) >= .35 else 0., **kwargs)

    movement.robot.move_velocity = gait
    pose = dict(movement.start)
    bearing = pose['heading'] - math.radians(16.127688)
    before = {'pose': pose, 'odom_session_id': 'map', 'frame_id': 1,
              'received_at': time.monotonic(), 'near_destination': False,
              'cargo_location': {'x': pose['x'] + 2.5 * math.cos(bearing),
                                 'y': pose['y'] + 2.5 * math.sin(bearing),
                                 'source': 'visual_distance_estimate'}}

    async def dispatch(name, params, *, context):
        assert name == 'go2_move' and context is movement.context
        return await go2_move(**params)

    async def observe(*args, **kwargs):
        return {**before, 'frame_id': 2, 'received_at': time.monotonic(),
                'pose': dict(movement.robot.odom)}

    monkeypatch.setattr(executor, 'execute_tool', dispatch)
    monkeypatch.setattr(cargo_push, 'observe_cargo', observe)
    with use_context(movement.context):
        result = await cargo_push._visual_step('盒子', None, 1.5, before,
            movement.context, FileEffectLedger(), ('go2', 'test', 'run', 'align'))
    assert result['result'] == 'ok' and result['progress'] == 'aligned', result
    assert result['movement']['direction'] == 'turn_right'
    assert abs(result['movement']['angle_deg'] - 16.127688) <= 3
    assert result['stop']['result'] == 'ok'
    active = [(vx, vy, yaw) for vx, vy, yaw in movement.commands if yaw]
    assert active and all(vx == vy == 0 and -.8 <= yaw < 0 for vx, vy, yaw in active)
    assert max(abs(yaw) for _, _, yaw in active) >= .35


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['forward', 'right'])
async def test_short_move_keeps_requested_speed_above_ineffective_gait_input(movement, direction):
    from cloud.go2.tools.tools import go2_move
    movement.fault['minimum_linear_speed'] = .2
    with use_context(movement.context):
        result = await go2_move(direction, distance_m=.15, speed=.25)
    assert result['result'] == 'ok', result
    assert .1 <= result['distance_m'] <= .2
    assert result['stop']['result'] == 'ok'
    active = [(vx, vy) for vx, vy, _ in movement.commands if vx or vy]
    assert active and max(math.hypot(vx, vy) for vx, vy in active) == pytest.approx(.25)
    assert all(math.hypot(vx, vy) <= .250001 for vx, vy in active)
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('angle_deg', [-5., 5., -25., 30., -43., 43.])
@pytest.mark.parametrize('relative', [False, True])
async def test_turn_reaches_heading_through_native_packets(movement, angle_deg, relative):
    """Both turn entries must work when low angular commands have no effect."""
    import json
    from cloud.go2.move_control_checks import robot_at_stand
    from unitree_webrtc_connect.constants import RTC_TOPIC

    robot, _, packets = robot_at_stand()
    simulated_move = movement.robot.move_velocity

    def receive(topic, *, data, **_):
        # Simulate walking only through the native velocity interface. Wireless
        # sticks depend on firmware mode and cannot prove an odom turn.
        assert topic == RTC_TOPIC['SPORT_MOD']
        if data['header']['identity']['api_id'] == 1008:
            velocity = json.loads(data['parameter'])
            # Model ineffective low angular commands near the target heading.
            yaw = velocity['z'] if abs(velocity['z']) >= .35 else 0.
            simulated_move(velocity['x'], velocity['y'], yaw)
        else:
            assert data['header']['identity']['api_id'] == 1003
            simulated_move(0, 0, 0)

    packets.side_effect = receive
    movement.robot.move_velocity = robot.move_velocity
    goal = movement.start['heading'] + math.radians(angle_deg)
    try:
        if relative:
            from cloud.go2.tools.tools import go2_move
            with use_context(movement.context):
                result = await go2_move('turn_left' if angle_deg > 0 else 'turn_right',
                                        angle_deg=abs(angle_deg))
            assert result['result'] == 'ok', result
        else:
            result = await movement.nav.go_to_coordinate(
                movement.start['x'], movement.start['y'], heading=goal,
                holonomic=True, speed_limit=.30, minimum_command_speed=.25)
            assert result['status'] == 'succeeded', result
    finally:
        if robot.is_connected:
            robot.move_velocity(0, 0, 0)
    assert abs(math.degrees(movement.robot.odom['heading'] - goal)) <= (3. if relative else 10.)
    assert all(vx == vy == 0 for vx, vy, _ in movement.commands)
    if relative or abs(angle_deg) > 10.:
        assert .35 <= max(abs(yaw) for _, _, yaw in movement.commands) <= .8
    else:
        assert all(yaw == 0 for _, _, yaw in movement.commands)
    assert movement.commands[-1] == (0, 0, 0)
    assert robot._move_control_active is False


@pytest.mark.asyncio
async def test_explicit_turn_speed_uses_angular_axis_and_stop_feedback(movement):
    from cloud.go2.tools.tools import go2_move
    with use_context(movement.context):
        result = await go2_move('turn_right', angle_deg=30, speed=.7)
    assert result['result'] == 'ok', result
    assert abs(result['angle_deg'] - 30) <= 3
    # Acceleration and braking may keep a short turn below its speed limit.
    assert .35 <= max(abs(yaw) for _, _, yaw in movement.commands) <= .7
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_stalled_turn_still_stops_without_claiming_success(movement):
    from cloud.go2.tools.tools import go2_move
    movement.fault['stall'] = True
    with use_context(movement.context):
        result = await go2_move('turn_right', angle_deg=30)
    assert result['result'] == 'blocked'
    assert result['reason'] == 'motion_not_observed'
    assert result['angle_deg'] == 0
    assert result['stop']['result'] == 'ok'
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('drift, expected', [(math.radians(4), 'succeeded'),
                                           (math.radians(12), 'failed')])
async def test_coordinate_arrival_uses_coarse_heading_after_braking(movement, drift, expected):
    original = movement.robot.move_velocity
    turned = False
    goal = movement.start['heading'] + math.radians(20.5)

    def brake_drift(vx, vy, yaw, **kwargs):
        nonlocal turned
        if yaw:
            turned = True
        elif turned and not any((vx, vy, yaw)):
            movement.robot.odom['heading'] = goal - drift
            turned = False
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = brake_drift
    result = await movement.nav.go_to_coordinate(
        movement.start['x'], movement.start['y'],
        heading=goal, holonomic=True)
    assert result['status'] == expected
    assert result['reason'] == ('arrival_unconfirmed' if expected == 'failed' else None)
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_arrival_corrects_one_braking_heading_drift_without_restarting_route(movement):
    """18:33/18:34 stopped inside XY tolerance, but failed heading_ok."""
    original = movement.robot.move_velocity
    goal = movement.start['heading'] + .8
    braking_drift = {'turned': False, 'injected': False}

    def move(vx, vy, yaw, **kwargs):
        if yaw:
            braking_drift['turned'] = True
        elif braking_drift['turned'] and not braking_drift['injected']:
            movement.robot.odom['heading'] = goal - math.radians(12)
            braking_drift['injected'] = True
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = move
    result = await movement.nav.go_to_coordinate(
        movement.start['x'], movement.start['y'], heading=goal)
    assert braking_drift['injected']
    assert result['status'] == 'succeeded', result
    assert abs(movement.robot.odom['heading'] - goal) <= math.radians(10)
    assert all(vx == vy == 0 for vx, vy, _ in movement.commands)
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', [.04, .12])
async def test_arrival_turn_never_chases_waypoint_after_position_drift(movement, drift):
    """Replay the .1 m arrival boundary: rotation must not restart translation."""
    pose = movement.robot.odom
    pose.update(x=1.694, y=1.023, heading=-1.06)
    original = movement.robot.move_velocity
    drifted = False

    def move(vx, vy, yaw, **kwargs):
        nonlocal drifted
        if yaw and not drifted:
            pose['y'] += drift
            drifted = True
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = move
    result = await movement.nav.go_to_coordinate(
        1.694, 1.023, face_target=(2.523, 1.027), arrival_tolerance_m=.1,
        speed_limit=.25, minimum_command_speed=.25)
    assert drifted
    assert all(vx == vy == 0 for vx, vy, _ in movement.commands)
    assert result['status'] == ('succeeded' if drift < .1 else 'failed'), result
    if drift > .1:
        assert result['reason'] == 'arrival_unconfirmed'
        assert result['diagnostics']['near_goal'] is False
        assert result['diagnostics']['still'] is True
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
async def test_explicit_relative_turn_keeps_precise_heading_after_braking(movement):
    from cloud.go2.tools.tools import go2_move
    original = movement.robot.move_velocity
    turned = False
    goal = movement.start['heading'] + math.radians(20.5)

    def brake_drift(vx, vy, yaw, **kwargs):
        nonlocal turned
        if yaw:
            turned = True
        elif turned and not any((vx, vy, yaw)):
            movement.robot.odom['heading'] = goal - math.radians(4)
            turned = False
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = brake_drift
    with use_context(movement.context):
        result = await go2_move('turn_left', angle_deg=20.5)
    assert result['result'] == 'blocked'
    assert result['reason'] == 'arrival_unconfirmed'
    assert result['stop']['result'] == 'ok'
    assert movement.commands[-1] == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('fresh_stop', [True, False])
async def test_coarse_cargo_arrival_accepts_settled_position_but_requires_real_stop(movement, fresh_stop):
    """18:44 reached the box, then failed solely on an 11 cm XY residual."""
    pose = movement.robot.odom
    pose.update(x=.26, y=1.617, heading=0.)
    movement.robot.occupancy_grid.grid = np.zeros((160, 160), dtype=bool)
    goal_x = 2.562
    original = movement.robot.move_velocity
    braking = {'moving': False, 'index': None}

    def move(vx, vy, yaw, **kwargs):
        if vx:
            braking['moving'] = True
        elif braking['moving'] and braking['index'] is None:
            pose.update(x=goal_x-.11, y=1.617)
            braking['index'] = len(movement.commands)
            if not fresh_stop:
                movement.fault['stale_after'] = 0
        return original(vx, vy, yaw, **kwargs)

    movement.robot.move_velocity = move
    result = await movement.nav.go_to_coordinate(
        goal_x, 1.617, coarse_arrival=True, face_target=(3.4, 1.617),
        speed_limit=.25, minimum_command_speed=.25)
    assert braking['index'] is not None
    assert result['status'] == ('succeeded' if fresh_stop else 'failed'), result
    assert all(cmd == (0, 0, 0) for cmd in movement.commands[braking['index']:])
    if not fresh_stop:
        assert result['reason'] == 'arrival_unconfirmed'
        assert result['diagnostics']['samples'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('distance, tolerance, needs_motion', [(.12, .15, False), (.20, .15, True),
                                                            (.20, None, False)])
async def test_coordinate_arrival_accepts_point_nearby_with_fresh_stop(movement, distance, tolerance, needs_motion):
    result = await movement.nav.go_to_coordinate(
        movement.start['x'] + distance * math.cos(movement.start['heading']),
        movement.start['y'] + distance * math.sin(movement.start['heading']),
        heading=movement.start['heading'] + math.radians(4), arrival_tolerance_m=tolerance)
    assert result['status'] == 'succeeded'
    assert any(vx or vy for vx, vy, _ in movement.commands) is needs_motion
    assert all(yaw == 0 for _, _, yaw in movement.commands)
    assert movement.commands[-1] == (0, 0, 0)


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
