"""Push receipts and delivery separation using fake camera, LiDAR and motion."""
import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import numpy as np

from cloud.go2.navigation import cargo_push as push
from cloud.go2.navigation.cargo_contact import CargoContact, claim_contact, permit_contact
from cloud.go2.navigation.occupancy_grid import OccupancyGrid
from cloud.go2.perception import cargo as perception
from cloud.go2.perception.visual_target import TargetUnlocalized
from cloud.space.execution import ExecutionContext, use_context


@pytest.fixture
def nudge(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    pose = {'x': 0., 'y': 0., 'z': .3, 'orientation': [0, 0, 0, 1],
            'heading': 0., 'session_id': 'odom-test'}
    cargo_points = [[x, y, z] for x in (.60, .65, .70)
                    for y in (-.025, .025) for z in (.20, .25)]
    floor = [[x, y, 0.] for x in np.arange(-1., 1.1, .1) for y in np.arange(-1., 1.1, .1)]
    def build_grid(extra=(), shift=0., cargo=True):
        shifted = [[x+shift, y, z] for x, y, z in cargo_points] if cargo else []
        return OccupancyGrid({'data': {'resolution': .05, 'origin': [-2., -2., -.5],
            'width': [100, 100, 40], 'header': {'frame_id': 'odom'},
            'data': {'points': np.asarray([*floor, *shifted, *extra])}}}, robot_pose=pose)
    samples = []
    for index in range(4):
        samples.append({
            'odom_session_id': 'odom-test', 'frame_id': index + 1,
            'received_at': time.monotonic() + index, 'pose': dict(pose),
            'cargo_location': {'x': .65, 'y': 0., 'uncertainty_m': .1},
            'cargo_bbox_1000': [400, 550, 600, 1000],
            'contact_observation': {'points_odom': cargo_points, 'pose': dict(pose),
                                    'frame': {'width': 640, 'height': 360}},
            'destination_anchor_1000': None, 'near_destination': False,
            'destination_precision': 'approximate_visual_proximity',
            'cluster_extent_xyz_m': [.15, .15, .15], 'cluster_points': 12,
            'voxel_resolution_m': .05})
    for sample in samples[2:]:
        sample['pose']['x'] = .15
        sample['cargo_location']['x'] += .15
    observe = AsyncMock(side_effect=samples)
    move = AsyncMock(return_value={'result': 'ok', 'stop': {'result': 'ok'}})
    grid = build_grid()
    monkeypatch.setattr(push, 'observe_cargo', observe)
    monkeypatch.setattr(push, '_authorize', lambda *_: None)
    async def authorized(operation, _context):
        return await operation
    monkeypatch.setattr(push, '_await_authorized', authorized)
    monkeypatch.setattr(push.navigator, 'snapshot', lambda: ((dict(pose), grid), None))
    monkeypatch.setattr('cloud.go2.tools.executor.execute_tool', move)
    monkeypatch.setattr(push, '_stop_approach', AsyncMock(return_value={'result': 'ok'}))
    return SimpleNamespace(samples=samples, observe=observe, move=move, grid=grid,
                           build_grid=build_grid, pose=pose,
                           context=ExecutionContext('push-test', 'admin', False))


@pytest.mark.asyncio
async def test_basic_nudge_confirms_cargo_displacement_without_delivery_region(nudge):
    with use_context(nudge.context), push.expect_cargo(None, request_id='single-nudge'):
        result = await push.push_cargo('眼前带白色图案的深色盒子')
        repeated = await push.push_cargo('眼前带白色图案的深色盒子')
    assert result['result'] == 'ok' and result['progress'] == 'pushed'
    assert result['confirmation'] == 'measured_cargo_displacement_and_stop'
    assert result['cargo_forward_displacement_m'] == .15
    assert result['delivered'] is False
    assert 'contact_observation' not in result['cargo_before']
    assert 'contact_observation' not in result['cargo_after']
    assert nudge.move.await_args.args == ('go2_move', {
        'direction': 'forward', 'distance_m': .15, 'speed': .25})
    assert nudge.move.await_count == 1 and repeated['replayed'] is True
    assert all(call.args[1] is None for call in nudge.observe.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', [False, True])
async def test_incomplete_push_preserves_measured_motion_and_stop_certainty(nudge, uncertain):
    nudge.move.return_value = {
        'result': 'accepted' if uncertain else 'blocked', 'reason': 'motion_not_observed',
        'distance_m': .046, 'requested_distance_m': .15,
        'stop': {'result': 'accepted' if uncertain else 'ok'},
        'message': '向前移动约0.05米，目标0.15米。未完成本次目标，已确认停稳。'}
    with use_context(nudge.context):
        result = await push.push_cargo('盒子')
    assert result['result'] == ('accepted' if uncertain else 'blocked')
    assert result['reason'] == 'cargo_move_unconfirmed'
    assert result['movement']['distance_m'] == .046
    assert result['delivered'] is False
    if not uncertain:
        assert '目标0.15米' in result['message'] and '已确认停稳' in result['message']
        assert '尚未确认货物发生位移' in result['message']
    else:
        assert '前进或停稳结果尚未确认' in result['message']


@pytest.mark.asyncio
async def test_robot_advance_does_not_prove_cargo_movement(nudge):
    for sample in nudge.samples[2:]:
        sample['cargo_location']['x'] = .65
    with use_context(nudge.context):
        result = await push.push_cargo('盒子')
    assert result['progress'] == 'approached'
    assert result['cargo_displacement_m'] == 0 and result['delivered'] is False


@pytest.mark.asyncio
async def test_camera_approach_alone_cannot_complete_delivery(nudge):
    for sample in nudge.samples:
        sample['destination_anchor_1000'] = [500, 400]
    for sample in nudge.samples[2:]:
        sample['cargo_location']['x'] = .65
        sample['near_destination'] = True
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '蓝色小方标')
    assert result['progress'] == 'approached' and result['delivered'] is False
    assert result['cargo_displacement_m'] == 0


@pytest.mark.asyncio
async def test_contact_block_still_prevents_all_motion(nudge, monkeypatch):
    grid = nudge.build_grid(extra=[[.35, .15, .30]])
    monkeypatch.setattr(push.navigator, 'snapshot', lambda: ((dict(nudge.pose), grid), None))
    with use_context(nudge.context):
        result = await push.push_cargo('盒子')
    assert result['result'] == 'blocked' and result['reason'] == 'cargo_contact_path_blocked'
    nudge.move.assert_not_awaited()


def test_contact_allows_only_measured_cargo_without_changing_shared_map(nudge):
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context)
    obstacles = nudge.grid.obstacles.copy()
    view = contact.grid_for(nudge.grid, nudge.pose)
    assert nudge.grid.is_body_clear(.25, 0., 0.) is False
    assert view.is_body_clear(.25, 0., 0.) is True
    assert view.is_body_clear(.31, 0., 0.) is False
    assert view.is_body_clear(.10, .08, 0.) is False
    assert np.array_equal(nudge.grid.obstacles, obstacles)


def test_contact_tracks_forward_cargo_motion_in_fresh_voxels(nudge):
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context)
    shifted = nudge.build_grid(shift=.10)
    view = contact.grid_for(shifted, {**nudge.pose, 'x': .10})
    assert view.is_body_clear(.25, 0., 0.) is True


def test_contact_uses_latest_measurement_from_real_session_map(nudge):
    from cloud.go2.navigation.session_map import SessionMap
    maps = SessionMap()
    maps.update(nudge.grid.voxel_message, nudge.pose, session_id='odom-test', observed_at=1.)
    shifted = nudge.build_grid(shift=.10)
    current = maps.update(shifted.voxel_message, {**nudge.pose, 'x': .10},
                          session_id='odom-test', observed_at=2.)
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context)
    view = contact.grid_for(current, {**nudge.pose, 'x': .10})
    assert current.voxel_message is shifted.voxel_message
    assert view.is_body_clear(.25, 0., 0.) is True
    assert current.obstacles.any()


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_contact', [False, True])
async def test_native_move_loop_reaches_contact_goal_only_with_measured_allowance(nudge, monkeypatch, allow_contact):
    from cloud.go2.navigation import navigator as nav_module
    from cloud.go2.navigation.session_map import SessionMap
    from cloud.go2.control import ControlLease
    from cloud.go2.tools import executor
    from cloud.go2.tools.tools import go2_move
    nav = nav_module.Navigator()
    maps, pose, commands = SessionMap(), dict(nudge.pose), []
    robot = SimpleNamespace(is_connected=True, _odom_session_id='odom-test', odom=pose,
        odom_frame_id='odom', voxel_frame_id='odom')
    def refresh():
        stamp = time.monotonic()
        # Simulate a parcel moving only once the robot reaches contact range.
        grid = nudge.build_grid(shift=max(0., pose['x']-.18))
        robot.occupancy_grid = maps.update(grid.voxel_message, pose,
            session_id='odom-test', observed_at=stamp)
        robot.odom_received_at = robot.voxel_received_at = stamp
    def move(vx, vy, yaw, **_kwargs):
        commands.append((vx, vy, yaw))
        pose['x'] += vx*.1
    real_sleep = asyncio.sleep
    async def tick(_delay):
        await real_sleep(.001)
        refresh()
    robot.move_velocity = move
    refresh()
    monkeypatch.setattr(nav_module, 'go2', robot)
    monkeypatch.setattr(nav_module, 'navigator', nav)
    monkeypatch.setattr(nav_module, 'runtime_for', lambda _: None)
    monkeypatch.setattr(nav_module, 'control_lease', ControlLease())
    monkeypatch.setattr(nav_module.asyncio, 'sleep', tick)
    monkeypatch.setattr(executor, 'execute_action', AsyncMock(return_value={'result': 'accepted'}))
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context) if allow_contact else None
    with use_context(nudge.context), permit_contact(contact):
        result = await go2_move('forward', distance_m=.25, speed=.25)
    assert result['result'] == ('ok' if allow_contact else 'blocked'), result
    assert result['stop']['result'] == 'ok' and commands[-1] == (0, 0, 0)
    if allow_contact:
        assert .20 <= result['distance_m'] <= .30
        assert contact.claimed is True
    assert robot.occupancy_grid.obstacles.any()


@pytest.mark.parametrize('obstacle', [[.95, 0., .25], [.65, 0., .50]])
def test_contact_keeps_other_obstacles_in_cargo_sweep_including_same_xy(nudge, obstacle):
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context)
    with pytest.raises(TargetUnlocalized, match='cargo_contact_path_blocked'):
        contact.grid_for(nudge.build_grid(extra=[obstacle]), nudge.pose)


def test_contact_does_not_exempt_a_neighboring_body_obstacle(nudge):
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context)
    view = contact.grid_for(nudge.build_grid(extra=[[.35, .15, .30]]), nudge.pose)
    assert view.is_body_clear(.15, 0., 0.) is False


@pytest.mark.parametrize('change', ['missing', 'jumped', 'session', 'expired'])
def test_contact_stops_when_target_or_scope_cannot_be_confirmed(nudge, change):
    contact = CargoContact(nudge.samples[1], nudge.pose, .25, nudge.context)
    grid, pose = nudge.grid, dict(nudge.pose)
    if change == 'missing':
        grid = nudge.build_grid(cargo=False)
    elif change == 'jumped':
        grid = nudge.build_grid(shift=.25)
    elif change == 'session':
        pose['session_id'] = 'other-session'
    else:
        contact.until = time.monotonic() - 1
    with pytest.raises(TargetUnlocalized, match='cargo_contact_'):
        contact.grid_for(grid, pose)


@pytest.mark.parametrize('direction,distance,speed', [('backward', .15, .25),
    ('forward', .25, .25), ('forward', .15, .30)])
def test_contact_cannot_be_repurposed_for_another_motion(nudge, direction, distance, speed):
    contact = CargoContact(nudge.samples[1], nudge.pose, .15, nudge.context)
    with permit_contact(contact), pytest.raises(ValueError, match='cargo_contact_scope_mismatch'):
        claim_contact(direction, distance, speed, nudge.context)


def test_contact_is_consumed_once_and_does_not_leak_to_ordinary_move(nudge):
    contact = CargoContact(nudge.samples[1], nudge.pose, .15, nudge.context)
    with permit_contact(contact):
        assert claim_contact('forward', .15, .25, nudge.context) is contact
        assert claim_contact('forward', .15, .25, nudge.context) is None
    assert claim_contact('forward', .15, .25, nudge.context) is None


@pytest.mark.asyncio
async def test_missing_cargo_measurement_never_falls_back_to_blind_move(nudge):
    nudge.observe.side_effect = TargetUnlocalized('cargo_returns_unavailable')
    with use_context(nudge.context):
        result = await push.push_cargo('扁盒子')
    assert result['reason'] == 'cargo_returns_unavailable' and result['movement'] is None
    nudge.move.assert_not_awaited()


@pytest.mark.asyncio
async def test_game_destination_cannot_be_dropped_for_basic_nudge(nudge):
    with use_context(nudge.context), push.expect_cargo({'cargo': '盒子', 'destination': '红地垫'}):
        result = await push.push_cargo('盒子')
    assert result['reason'] == 'cargo_delivery_contract_changed'
    nudge.observe.assert_not_awaited()
    nudge.move.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_still_requires_destination_geometry(nudge):
    nudge.observe.side_effect = TargetUnlocalized('destination_geometry_invalid')
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '蓝色小方标')
    assert nudge.observe.await_args.args == ('盒子', '蓝色小方标')
    assert result['reason'] == 'destination_geometry_invalid' and result['delivered'] is False
    nudge.move.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('both_frames_near', [True, False])
async def test_delivery_uses_stopped_cargo_proximity_in_two_new_frames(nudge, both_frames_near):
    for sample in nudge.samples:
        sample['destination_anchor_1000'] = [500, 400]
    nudge.samples[2]['near_destination'] = True
    nudge.samples[3]['near_destination'] = both_frames_near
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '蓝色小方标')
    assert result['delivered'] is both_frames_near
    assert result['confirmation'] == ('cargo_near_destination_and_stopped' if both_frames_near
                                      else 'measured_cargo_displacement_and_stop')
    assert result['stop']['result'] == 'ok'


@pytest.mark.asyncio
async def test_cargo_already_near_destination_stops_without_an_extra_push(nudge):
    for sample in nudge.samples[:2]:
        sample['near_destination'] = True
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '桌腿旁边')
    assert result['delivered'] is True and result['destination_precision'] == 'approximate_visual_proximity'
    nudge.move.assert_not_awaited()


@pytest.mark.parametrize('kind,box,near', [
    ('marker', [700, 840, 720, 860], True),  # about 13x7px; no containment
    ('marker', [365, 570, 385, 590], False),  # live log: recognized but still far
    ('object', [700, 300, 850, 800], True),
    ('object', [600, 50, 800, 300], False),  # overlapping silhouette, distant base
    ('region', [0, 500, 1000, 1000], True),  # incomplete visible boundary allowed
])
def test_small_markers_and_object_bases_use_approximate_cargo_proximity(kind, box, near):
    frame = {'width': 640, 'height': 360}
    result = perception._destination_geometry(frame, {
        'cargo_bbox_1000': [500, 650, 700, 850],
        'destination_kind': kind, 'destination_bbox_1000': box})
    assert result['near_destination'] is near
    assert result['destination_precision'] == 'approximate_visual_proximity'


@pytest.mark.asyncio
@pytest.mark.parametrize('destination,visible_destination', [
    (None, False), ('蓝色小方标', False), ('蓝色小方标', True)])
@pytest.mark.parametrize('associated', [True, False])
async def test_cargo_and_destination_perception_share_measured_localization(monkeypatch, destination, visible_destination, associated):
    from cloud.go2.perception import rgb_lidar
    from cloud.go2.navigation import occupancy_grid
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'odom-test'}
    frame = {'pose_at_capture': pose, 'odom_session_id': 'odom-test', 'frame_id': 1,
             'received_at': time.monotonic(), 'width': 640, 'height': 360}
    robot = SimpleNamespace(body_state='standing', _sport_state_observed_at=time.monotonic(),
        voxel_raw={'data': {'resolution': .05}}, voxel_received_at=time.monotonic(),
        odom_frame_id='odom', voxel_frame_id='odom')
    monkeypatch.setattr(perception, 'go2', robot)
    monkeypatch.setattr(occupancy_grid, 'OccupancyGrid', lambda *_a, **_k:
                        SimpleNamespace(measured_floor_z=0.))
    monkeypatch.setattr(perception.visual_target, '_capture_still', AsyncMock(return_value=(b'jpeg', frame, 1.)))
    monkeypatch.setattr(perception.visual_target, 'validate_samples', lambda *_: None)
    monkeypatch.setattr(perception.visual_target, '_live_pose', lambda *_: dict(pose))
    detected = {'cargo_count': 1, 'cargo_ground_contact_visible': True,
                'cargo_bbox_1000': [450, 600, 550, 800]}
    if visible_destination:
        detected.update(destination_count=1, destination_ground_contact_visible=True,
                        destination_kind='marker', destination_bbox_1000=[555, 790, 575, 810])
    ask = AsyncMock(return_value=detected)
    monkeypatch.setattr(perception.visual_target, '_ask_vision', ask)
    geometry = Mock(wraps=perception._destination_geometry)
    monkeypatch.setattr(perception, '_destination_geometry', geometry)
    monkeypatch.setattr(rgb_lidar, 'go2_camera_profile', lambda *_: {})
    events = []
    monkeypatch.setattr(perception, 'emit_agent_event', lambda event, **fields: events.append((event, fields)))
    def associate(*_args, diagnostics, target_points):
        diagnostics['components'] = [{'extent_xyz_m': [.15] * 3, 'points': 12}]
        diagnostics['reason'] = 'associated' if associated else 'insufficient_foreground_returns'
        return {'x': .65, 'y': 0., 'uncertainty_m': .1} if associated else None
    monkeypatch.setattr(rgb_lidar, 'associate_lidar_target', associate)
    if destination is not None and not visible_destination:
        with pytest.raises(TargetUnlocalized, match='cargo_or_destination_ambiguous'):
            await perception.observe_cargo('深色盒子', destination)
        assert not events
    elif not associated:
        with pytest.raises(TargetUnlocalized, match='cargo_returns_unavailable'):
            await perception.observe_cargo('深色盒子', destination)
    else:
        result = await perception.observe_cargo('深色盒子', destination)
        assert result['cargo_location']['source'] == 'lidar_foreground_cluster'
        assert result['near_destination'] is visible_destination
        assert ask.await_args.args[-1] == ('cargo_delivery_geometry' if visible_destination else 'cargo_geometry')
    if events:
        assert events[-1][0] == 'vision.lidar'
        assert events[-1][1]['matched'] is associated
        assert events[-1][1]['association_diagnostics']['reason'] == (
            'associated' if associated else 'insufficient_foreground_returns')
    assert geometry.call_count == int(visible_destination)
