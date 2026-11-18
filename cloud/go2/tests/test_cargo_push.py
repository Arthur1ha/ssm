"""Push receipts and delivery separation using fake camera, LiDAR and motion."""
import time
import asyncio
from copy import deepcopy
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import numpy as np

from cloud.go2.navigation import cargo_push as push
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
    for index in range(2):
        samples.append({
            'odom_session_id': 'odom-test', 'frame_id': index + 1,
            'received_at': time.monotonic() + index, 'pose': dict(pose),
            'cargo_location': {'x': .65, 'y': 0., 'uncertainty_m': .1},
            'cargo_bbox_1000': [400, 550, 600, 1000],
            'destination_anchor_1000': None, 'near_destination': False,
            'destination_location': {'x': 2., 'y': 0., 'precision': 'approximate'},
            'destination_precision': 'approximate_visual_proximity',
            'cluster_extent_xyz_m': [.15, .15, .15], 'cluster_points': 12,
            'voxel_resolution_m': .05, 'advance_distance_m': .15})
    for sample in samples[1:]:
        sample['pose']['x'] = .15
        sample['cargo_location']['x'] += .15
    observe = AsyncMock(side_effect=samples)
    move = AsyncMock(return_value={'result': 'ok', 'stop': {'result': 'ok'}})
    grid = build_grid()
    monkeypatch.setattr(push, 'observe_cargo', observe)
    monkeypatch.setattr(push, '_authorize', lambda *_: None)
    async def authorized(operation, _context, *, on_result=None):
        result = await operation
        if on_result:
            on_result(result)
        return result
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
    assert nudge.observe.await_count == 2
    assert not nudge.observe.await_args_list[0].kwargs
    assert nudge.observe.await_args_list[1].kwargs['after_frame_id'] == nudge.samples[0]['frame_id']
    assert 'after_received_at' in nudge.observe.await_args_list[1].kwargs
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
    for sample in nudge.samples[1:]:
        sample['cargo_location']['x'] = .65
    with use_context(nudge.context):
        result = await push.push_cargo('盒子')
    assert result['progress'] == 'approached'
    assert result['cargo_displacement_m'] == 0 and result['delivered'] is False


@pytest.mark.asyncio
async def test_camera_approach_alone_cannot_complete_delivery(nudge):
    for sample in nudge.samples:
        sample['destination_anchor_1000'] = [500, 400]
    for sample in nudge.samples[1:]:
        sample['cargo_location']['x'] = .65
        sample['near_destination'] = True
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '蓝色小方标')
    assert result['progress'] == 'push_attempted' and result['delivered'] is False
    assert result['cargo_motion_confirmed'] is False
    assert 'cargo_displacement_m' not in result
    assert nudge.observe.await_count == 1


@pytest.mark.asyncio
async def test_confirmed_target_uses_fresh_lidar_without_reasking_vision(nudge, monkeypatch):
    monkeypatch.setattr('cloud.go2.navigation.occupancy_grid.OccupancyGrid',
                        Mock(side_effect=AssertionError('cargo must reuse the shared map')))
    sample = nudge.samples[0]
    sample['cargo_location']['source'] = 'lidar_foreground_cluster'
    sample.update(near_destination=True, advance_distance_m=3.5)
    sample.update(capture_pose=dict(nudge.pose), image_size={'width': 640, 'height': 360})
    reference = push._reference_for(sample, '带白圆标的盒子', '蓝色点位')
    robot = SimpleNamespace(body_state='standing', body_ready_reason=lambda: None,
        _sport_state_observed_at=time.monotonic(), voxel_raw=nudge.grid.voxel_message,
        voxel_received_at=time.monotonic(), voxel_frame_id='odom', _odom_session_id='odom-test',
        occupancy_grid=nudge.grid)
    nudge.grid.observed_at, nudge.grid.session_id = time.monotonic(), 'odom-test'
    monkeypatch.setattr(perception, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, '_live_pose', lambda _: dict(nudge.pose))
    ask = AsyncMock(side_effect=AssertionError('confirmed cargo must use its reference'))
    monkeypatch.setattr(perception.visual_target, '_ask_vision', ask)
    with use_context(nudge.context):
        result = await perception.observe_cargo('盒子', '蓝点', reference=reference)
    assert result['tracking_source'] == 'lidar'
    assert result['cargo_location']['x'] == pytest.approx(.65)
    assert 'contact_observation' not in result
    assert result['near_destination'] is False
    assert 0 < result['advance_distance_m'] < sample['advance_distance_m']
    assert result['received_at'] == sample['received_at']  # Old image age remains explicit.
    ask.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('obstacle', [False, True])
@pytest.mark.parametrize('phase', ['approach', 'push'])
async def test_cargo_movement_uses_shared_map_and_ordinary_obstacle_checks(nudge, monkeypatch, obstacle, phase):
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
        grid = nudge.build_grid(cargo=obstacle)
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
    lease = ControlLease()
    lease.acquire(nudge.context)
    monkeypatch.setattr(nav_module, 'control_lease', lease)
    monkeypatch.setattr(nav_module.asyncio, 'sleep', tick)
    monkeypatch.setattr(executor, 'execute_action', AsyncMock(return_value={'result': 'accepted'}))
    with use_context(nudge.context):
        if phase == 'push':
            result = await go2_move('forward', distance_m=.25, speed=.25)
            assert result['result'] == ('blocked' if obstacle else 'ok'), result
            assert result['stop']['result'] == 'ok'
            if not obstacle:
                assert .20 <= result['distance_m'] <= .30
        else:
            result = await nav.go_to_coordinate(.25, 0., owner_session_id=nudge.context.session_id,
                heading=0., speed_limit=.25, arrival_tolerance_m=.05)
            assert result['status'] == ('blocked' if obstacle else 'succeeded'), result
            stopped = await nav.stop_and_confirm(owner_session_id=nudge.context.session_id)
            assert stopped['result'] == 'ok'
    assert commands[-1] == (0, 0, 0)
    assert bool(robot.occupancy_grid.obstacles.any()) is obstacle


@pytest.mark.asyncio
async def test_missing_cargo_measurement_never_falls_back_to_blind_move(nudge):
    nudge.observe.side_effect = TargetUnlocalized('cargo_returns_unavailable')
    with use_context(nudge.context):
        result = await push.push_cargo('扁盒子')
    assert result['reason'] == 'cargo_returns_unavailable' and result['movement'] is None
    nudge.move.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('destination', [None, '盒子左前方的同一个红地垫'])
async def test_original_order_is_retained_when_model_omits_or_rewords_it(nudge, destination):
    reference = push._reference_for(nudge.samples[0], '带白圆标的盒子', '红地垫')
    with use_context(nudge.context), push.expect_cargo(reference):
        result = await push.push_cargo('换位后右侧的盒子', destination)
    assert result['result'] == 'ok'
    assert nudge.observe.await_args.args == ('带白圆标的盒子', '红地垫')
    assert result['reference']['cargo'] == '带白圆标的盒子'
    assert result['reference']['destination'] == '红地垫'


@pytest.mark.asyncio
async def test_delivery_still_requires_destination_geometry(nudge):
    nudge.observe.side_effect = TargetUnlocalized('destination_geometry_invalid')
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '蓝色小方标')
    assert nudge.observe.await_args.args == ('盒子', '蓝色小方标')
    assert result['reason'] == 'destination_geometry_invalid' and result['delivered'] is False
    nudge.move.assert_not_awaited()


@pytest.mark.asyncio
async def test_directional_delivery_finishes_attempt_without_requiring_a_post_contact_frame(nudge):
    for sample in nudge.samples:
        sample['destination_anchor_1000'] = [500, 400]
    nudge.observe.side_effect = [nudge.samples[0], TargetUnlocalized('cargo_or_destination_ambiguous')]
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '蓝色小方标')
    assert result['delivered'] is False
    assert result['directional_push_attempted'] is True
    assert result['confirmation'] == 'directional_move_and_stop'
    assert result['stop']['result'] == 'ok'
    assert result['cargo_after'] is None and nudge.observe.await_count == 1


@pytest.mark.asyncio
async def test_cargo_already_near_destination_stops_without_an_extra_push(nudge):
    for sample in nudge.samples[:2]:
        sample['near_destination'] = True
    with use_context(nudge.context):
        result = await push.push_cargo('盒子', '桌腿旁边')
    assert result['delivered'] is True and result['destination_precision'] == 'approximate_visual_proximity'
    assert result['progress'] == 'already_near_destination'
    assert result['motion_performed'] is False and result['cargo_motion_confirmed'] is False
    assert result['movement'] is None and result['cargo_after'] is None
    assert '没有执行靠近或推送' in result['message']
    assert nudge.observe.await_count == 1
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
        'cargo_distance_m': 2., 'destination_distance_m': 2.,
        'destination_kind': kind, 'destination_bbox_1000': box})
    assert result['near_destination'] is near
    assert result['destination_precision'] == 'approximate_visual_proximity'


@pytest.mark.asyncio
@pytest.mark.parametrize('destination,visible_destination', [
    (None, False), ('蓝色小方标', False), ('蓝色小方标', True)])
@pytest.mark.parametrize('associated', [True, False, None])
@pytest.mark.parametrize('low_box', [False, True])
@pytest.mark.parametrize('refresh_during_inference', [False, True])
async def test_cargo_and_destination_perception_share_measured_localization(monkeypatch, destination, visible_destination, associated, low_box, refresh_during_inference):
    from cloud.go2.perception import rgb_lidar
    monkeypatch.setattr('cloud.go2.navigation.occupancy_grid.OccupancyGrid',
                        Mock(side_effect=AssertionError('cargo must reuse the shared map')))
    pose = {'x': 0., 'y': 0., 'z': .3, 'orientation': [0, 0, 0, 1],
            'heading': 0., 'session_id': 'odom-test'}
    frame = {'pose_at_capture': pose, 'odom_session_id': 'odom-test', 'frame_id': 1,
             'received_at': time.monotonic(), 'width': 640, 'height': 360}
    robot = SimpleNamespace(body_state='standing', _sport_state_observed_at=time.monotonic(),
        body_ready_reason=lambda: None,
        voxel_raw={'data': {'resolution': .05}}, voxel_received_at=time.monotonic(),
        odom_frame_id='odom', voxel_frame_id='odom')
    robot.occupancy_grid = SimpleNamespace(measured_floor_z=None if associated is None else 0.,
        observed_at=time.monotonic(), session_id='odom-test', frame_id='odom',
        voxel_message=robot.voxel_raw)
    monkeypatch.setattr(perception, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, '_capture_still', AsyncMock(return_value=(b'jpeg', frame, 1.)))
    monkeypatch.setattr(perception.visual_target, 'validate_samples', lambda *_: None)
    monkeypatch.setattr(perception.visual_target, '_live_pose', lambda *_: dict(pose))
    detected = {'cargo_count': 1, 'cargo_ground_contact_visible': True,
                'cargo_bbox_1000': [450, 750 if low_box else 350, 550, 800],
                'cargo_distance_m': 2.3, 'advance_distance_m': 1.4}
    if visible_destination:
        detected.update(destination_count=1, destination_ground_contact_visible=True,
                        destination_kind='marker', destination_bbox_1000=[555, 790, 575, 810],
                        destination_distance_m=2.3)
    ask = AsyncMock(return_value=detected)
    if refresh_during_inference:
        robot.voxel_received_at -= 10
        async def refreshed_scan(*_args):
            robot.voxel_raw = {'data': {'resolution': .05}, 'refreshed': True}
            robot.voxel_received_at = time.monotonic()
            robot.occupancy_grid.voxel_message = robot.voxel_raw
            robot.occupancy_grid.observed_at = robot.voxel_received_at
            return detected
        ask.side_effect = refreshed_scan
    monkeypatch.setattr(perception.visual_target, '_ask_vision', ask)
    geometry = Mock(wraps=perception._destination_geometry)
    monkeypatch.setattr(perception, '_destination_geometry', geometry)
    camera = rgb_lidar.go2_camera_profile(640, 360)
    monkeypatch.setattr(rgb_lidar, 'go2_camera_profile', lambda *_: camera)
    events = []
    monkeypatch.setattr(perception, 'emit_agent_event', lambda event, **fields: events.append((event, fields)))
    def associate(*_args, diagnostics):
        diagnostics['components'] = [{'extent_xyz_m': [.15] * 3, 'points': 12}]
        diagnostics['reason'] = 'associated' if associated else 'insufficient_foreground_returns'
        return {'x': .65, 'y': 0., 'uncertainty_m': .1} if associated else None
    monkeypatch.setattr(rgb_lidar, 'associate_lidar_target', associate)
    reference = ({'cargo': '深色盒子', 'destination': destination,
                  'odom_session_id': 'odom-test', 'cargo_location': {'x': .65, 'y': 0.}}
                 if refresh_during_inference else None)
    if destination is not None and not visible_destination:
        with pytest.raises(TargetUnlocalized, match='cargo_or_destination_ambiguous'):
            await perception.observe_cargo('深色盒子', destination, reference=reference)
        assert not events
    else:
        result = await perception.observe_cargo('深色盒子', destination, reference=reference)
        assert result['cargo_location']['source'] == ('lidar_foreground_cluster' if associated
                                                     else 'visual_distance_estimate')
        if not associated:
            assert 'contact_observation' not in result and 'cluster_points' not in result
        assert result['near_destination'] is visible_destination
        assert ask.await_args.args[-1] == ('cargo_delivery_geometry' if visible_destination else 'cargo_geometry')
    if reference:
        assert 'current_pose' in ask.await_args.args[1]
        assert '首次确认时的视角' in ask.await_args.args[3]
        assert '不得补画' in ask.await_args.args[3]
    if events:
        assert events[0][0] == 'vision.lidar'
        assert events[0][1]['matched'] is bool(associated)
        assert events[0][1]['association_diagnostics']['reason'] == (
            'associated' if associated else 'measured_floor_unavailable' if associated is None
            else 'insufficient_foreground_returns')
    assert geometry.call_count == int(visible_destination)


def test_foreground_marker_does_not_complete_delivery_of_background_box():
    # Exact geometry from the failed 15:01 conversation: image-space neighbors
    # can be meters apart on the ground.
    observed = {'cargo_bbox_1000': [546, 458, 592, 571],
                'destination_bbox_1000': [558, 593, 572, 610],
                'destination_kind': 'marker', 'cargo_distance_m': 4.5,
                'destination_distance_m': 2.3}
    frame = {'width': 640, 'height': 360}
    result = perception._destination_geometry(frame, observed)
    assert result['destination_gap_cargo_widths'] < 1
    assert result['near_destination'] is False and result['destination_depth_confirmed'] is False
    same_depth = perception._destination_geometry(frame, {**observed, 'destination_distance_m': 4.5})
    assert same_depth['near_destination'] is True


@pytest.mark.asyncio
async def test_wrong_candidate_is_checked_against_original_request_before_motion(nudge, monkeypatch):
    frame = {'pose_at_capture': nudge.pose, 'pose_received_at': time.monotonic(),
             'odom_session_id': 'odom-test', 'frame_id': 1, 'received_at': time.monotonic(),
             'width': 640, 'height': 360}
    robot = SimpleNamespace(body_state='standing', _sport_state_observed_at=time.monotonic(),
        body_ready_reason=lambda: None,
        voxel_raw=None, voxel_received_at=None, odom_frame_id='odom', voxel_frame_id=None)
    monkeypatch.setattr(perception, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, '_capture_still',
                        AsyncMock(return_value=(b'jpeg', frame, 1.)))
    vision = AsyncMock(return_value={'cargo_count': 1, 'cargo_matches_user_request': False,
                                    'cargo_match_reason': '候选是远处堆叠纸箱，近处另有独立盒子。'})
    monkeypatch.setattr(perception.visual_target, '_ask_vision', vision)
    monkeypatch.setattr(push, 'observe_cargo', perception.observe_cargo)
    original = '去把前面的纸盒子推到蓝色点位上'
    with use_context(nudge.context), perception.cargo_request(original):
        result = await push.push_cargo('远处桌腿旁的棕色纸箱', '蓝色点位')
    assert result['result'] == 'blocked' and result['reason'] == 'cargo_target_mismatch'
    nudge.move.assert_not_awaited()
    assert original in vision.await_args.args[1]
    assert '远处桌腿旁的棕色纸箱' in vision.await_args.args[1]


def test_visual_model_distance_is_not_restricted_by_height_or_floor():
    from cloud.go2.perception.rgb_lidar import go2_camera_profile
    pose = {'x': 0., 'y': 0., 'heading': 0.}
    location = perception._visual_cargo_location({'width': 640, 'height': 360}, pose,
        go2_camera_profile(640, 360), [450, 700, 550, 1000], {'cargo_distance_m': 7.3})
    assert location['source'] == 'visual_distance_estimate'
    assert location['estimated_distance_m'] == 7.3
    assert 'lidar_points' not in location




@pytest.fixture
def visual_nudge(nudge, monkeypatch):
    for i, sample in enumerate(nudge.samples):
        sample['received_at'] = time.monotonic() - 1 + i * .1
        sample['cargo_location'].update(source='visual_distance_estimate',
                                         precision='approximate', uncertainty_m=.3)
        sample.pop('cluster_extent_xyz_m')
        sample.pop('cluster_points')
    # Low cargo is below the shared body's obstacle cutoff. No map exemption
    # is needed, and no synthetic object points are inserted into that map.
    low_grid = nudge.build_grid(cargo=False)
    monkeypatch.setattr(push.navigator, 'snapshot', lambda: ((dict(nudge.pose), low_grid), None))
    return nudge


@pytest.mark.asyncio
@pytest.mark.parametrize('distance', [.07, 1.2])
async def test_visual_low_cargo_can_attempt_push_without_metric_cluster(visual_nudge, monkeypatch, distance):
    n = visual_nudge
    with use_context(n.context), push.expect_cargo(None, request_id='visual-low-box'):
        result = await push.push_cargo('七厘米的小盒子', distance_m=distance)
        repeated = await push.push_cargo('七厘米的小盒子', distance_m=distance)
    assert result['result'] == 'ok'
    assert result['progress'] in {'push_attempted', 'approached'}
    assert result['cargo_precision'] == 'approximate_visual'
    assert result['delivered'] is False and 'cargo_displacement_m' not in result
    assert result['stop']['result'] == 'ok'
    assert n.move.await_args.args == ('go2_move', {
        'direction': 'forward', 'distance_m': distance, 'speed': .25})
    assert repeated['replayed'] is True and n.move.await_count == 1
    assert n.observe.await_count == 2


@pytest.mark.asyncio
async def test_visual_cargo_turns_toward_box_as_one_step(visual_nudge):
    n = visual_nudge
    for sample in n.samples:
        sample['cargo_location']['y'] = .3
    with use_context(n.context):
        result = await push.push_cargo('偏左的小盒子')
    assert result['result'] == 'ok' and result['progress'] == 'aligned'
    name, params = n.move.await_args.args
    assert name == 'go2_move' and params['direction'] == 'turn_left'
    assert params['angle_deg'] == pytest.approx(24.7751405688) and 'distance_m' not in params
    assert 'speed' not in params


@pytest.mark.asyncio
async def test_visual_step_keeps_real_movement_when_cargo_reobservation_fails(visual_nudge):
    n = visual_nudge
    n.observe.side_effect = [n.samples[0], TargetUnlocalized('cargo_ambiguous')]
    with use_context(n.context):
        result = await push.push_cargo('小盒子')
    assert result['result'] == 'blocked' and result['reason'] == 'cargo_ambiguous'
    assert result['movement']['result'] == 'ok' and result['stop']['result'] == 'ok'
    assert result['delivered'] is False


@pytest.mark.asyncio
async def test_visual_step_does_not_claim_push_or_delivery_for_unknown_motion(visual_nudge):
    n = visual_nudge
    n.move.return_value = {'result': 'accepted', 'stop': {'result': 'accepted'}}
    with use_context(n.context):
        result = await push.push_cargo('小盒子')
    assert result['result'] == 'accepted' and result['reason'] == 'cargo_move_unconfirmed'
    assert result['delivered'] is False and 'progress' not in result
    assert n.observe.await_count == 1


@pytest.mark.asyncio
async def test_delivery_approaches_then_reobserves_before_positioning_and_pushing(visual_nudge, monkeypatch):
    """Replay the 15:42 geometry: start toward the box, not the lateral station."""
    n = visual_nudge
    sample = deepcopy(n.samples[0])
    sample.update(cargo_bbox_1000=[456, 548, 526, 626], advance_distance_m=.5)
    sample['cargo_location'].update(x=1.206, y=.020)
    sample['destination_location'].update(x=1.567, y=-.355)
    observations = []

    async def observe(*args, **kwargs):
        observations.append((args, kwargs))
        return {**deepcopy(sample), 'pose': dict(n.pose), 'frame_id': len(observations)}

    async def navigate(x, y, **kwargs):
        n.pose.update(x=x, y=y, heading=kwargs.get('heading', math.atan2(
            sample['cargo_location']['y']-y, sample['cargo_location']['x']-x)))
        return {'status': 'succeeded', 'last_trusted_pose': dict(n.pose)}

    n.observe.side_effect = observe
    navigation = AsyncMock(side_effect=navigate)
    monkeypatch.setattr(push.navigator, 'go_to_coordinate', navigation)
    with use_context(n.context), push.expect_cargo(None, request_id='approach-first'):
        approach = await push.push_cargo('正前方的盒子', '右后方蓝点', distance_m=1.1)
        replay = await push.push_cargo('正前方的盒子', '右后方蓝点', distance_m=1.1)
    assert approach['progress'] == 'approached' and approach['final'] is False
    assert approach['directional_push_attempted'] is False and approach['delivered'] is False
    assert approach['stop']['result'] == 'ok'
    assert replay['replayed'] is True and navigation.await_count == 1
    n.move.assert_not_awaited()
    x, y = navigation.await_args.args
    assert 0 < x < sample['cargo_location']['x'] and abs(y) < .02
    assert math.hypot(x-1.206, y-.020) > .7
    assert 'heading' not in navigation.await_args.kwargs
    assert 'route_keepout' not in navigation.await_args.kwargs

    # The closer view may revise the destination; do not stage using the old view.
    sample['destination_location'].update(y=-.5)
    with use_context(n.context), push.expect_cargo(approach['reference'], request_id='position-second'):
        position = await push.push_cargo('换个说法的盒子', distance_m=1.1)
    assert position['progress'] == 'positioned' and position['final'] is False
    assert observations[1][0] == ('正前方的盒子', '右后方蓝点')
    assert observations[1][1]['after_received_at'] == approach['reference']['reobserve_after']
    assert observations[1][1]['after_frame_id'] == 1
    assert navigation.await_count == 2
    assert navigation.await_args.args[1] > .4
    assert navigation.await_args.kwargs['heading'] < 0
    assert 'reobserve_after' not in position['reference']
    n.move.assert_not_awaited()

    with use_context(n.context), push.expect_cargo(position['reference'], request_id='push-third'):
        result = await push.push_cargo('盒子', distance_m=1.1)
    assert result['directional_push_attempted'] is True
    assert result['confirmation'] == 'directional_move_and_stop' and result['delivered'] is False
    assert navigation.await_count == 2
    n.move.assert_awaited_once_with('go2_move', {
        'direction': 'forward', 'distance_m': 1.1, 'speed': .25}, context=n.context)


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain_stop', [False, True])
async def test_failed_delivery_approach_cannot_position_or_push(visual_nudge, monkeypatch, uncertain_stop):
    n = visual_nudge
    n.samples[0]['cargo_location'].update(x=1.2, y=0.)
    n.samples[0]['cargo_bbox_1000'] = [456, 548, 526, 626]
    n.samples[0]['destination_location'].update(x=1.56, y=-.35)
    navigation = AsyncMock(return_value={
        'status': 'succeeded' if uncertain_stop else 'blocked',
        'reason': None if uncertain_stop else 'body_clearance_unavailable',
        'last_trusted_pose': {**n.pose, 'x': .2}})
    monkeypatch.setattr(push.navigator, 'go_to_coordinate', navigation)
    if uncertain_stop:
        push._stop_approach.return_value = {'result': 'accepted'}
    with use_context(n.context), push.expect_cargo(None, request_id='failed-approach'):
        result = await push.push_cargo('盒子', '蓝点')
        replay = await push.push_cargo('盒子', '蓝点')
    assert result['result'] == ('accepted' if uncertain_stop else 'blocked')
    assert result['directional_push_attempted'] is False
    assert 'progress' not in result and 'reobserve_after' not in result['reference']
    if not uncertain_stop:
        assert '已接近约0.20米' in result['message'] and '未执行向前推送' in result['message']
    assert replay['replayed'] is True and navigation.await_count == n.observe.await_count == 1
    n.move.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('target', [(1., 1.), (1., -1.), (-1., 0.)])
async def test_off_axis_destination_requires_positioning_before_any_push(visual_nudge, monkeypatch, target):
    n = visual_nudge
    n.samples[0]['destination_location'].update(x=target[0], y=target[1])
    navigation = AsyncMock(return_value={'status': 'succeeded', 'last_trusted_pose': n.pose})
    monkeypatch.setattr(push.navigator, 'go_to_coordinate', navigation)
    with use_context(n.context), push.expect_cargo(None, request_id='position-for-blue'):
        result = await push.push_cargo('脚边深色盒子', '蓝色点位')
        replay = await push.push_cargo('脚边深色盒子', '蓝色点位')
    assert result['progress'] == 'positioned' and result['directional_push_attempted'] is False
    assert result['delivered'] is False and n.observe.await_count == 1
    n.move.assert_not_awaited()
    assert navigation.await_count == 1 and replay['replayed'] is True
    assert navigation.await_args.kwargs['minimum_command_speed'] == .25
    x, y = navigation.await_args.args
    cargo = n.samples[0]['cargo_location']
    # The dog stages on the opposite side of the cargo from the blue marker.
    assert (x-cargo['x'])*(target[0]-cargo['x']) + (y-cargo['y'])*(target[1]-cargo['y']) < 0
    assert navigation.await_args.kwargs['route_keepout'][:2] == (cargo['x'], cargo['y'])
    assert 'keepout' not in navigation.await_args.kwargs
    assert result['final'] is False


@pytest.mark.asyncio
async def test_positioning_failure_cannot_fall_through_to_a_wrong_direction_push(visual_nudge, monkeypatch):
    n = visual_nudge
    n.samples[0]['destination_location']['y'] = 2.
    monkeypatch.setattr(push.navigator, 'go_to_coordinate', AsyncMock(return_value={
        'status': 'blocked', 'reason': 'no_known_free_path'}))
    with use_context(n.context):
        result = await push.push_cargo('盒子', '蓝标')
    assert result['result'] == 'blocked' and result['reason'] == 'no_known_free_path'
    assert result['directional_push_attempted'] is False
    n.move.assert_not_awaited()


@pytest.mark.asyncio
async def test_stalled_positioning_preserves_completed_movement_in_report(visual_nudge, monkeypatch):
    n = visual_nudge
    n.samples[0]['destination_location']['y'] = 2.
    pose = n.pose
    monkeypatch.setattr(push.navigator, 'go_to_coordinate', AsyncMock(return_value={
        'status': 'blocked', 'reason': 'motion_not_observed',
        'message': '运动指令未产生可确认的位移，已停止',
        'last_trusted_pose': {**pose, 'x': pose['x'] + 1.23}}))
    with use_context(n.context):
        result = await push.push_cargo('盒子', '蓝标')
    assert result['result'] == 'blocked' and result['reason'] == 'motion_not_observed'
    assert '已换位约1.23米' in result['message']
    assert '未执行向前推送' in result['message']
    assert result['directional_push_attempted'] is False and result['delivered'] is False
    n.move.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('uncertain', [False, True])
async def test_directional_attempt_keeps_motion_failure_and_uncertain_stop(visual_nudge, uncertain):
    n = visual_nudge
    n.move.return_value = {'result': 'accepted' if uncertain else 'blocked',
        'reason': 'stop_unconfirmed' if uncertain else 'motion_not_observed',
        'distance_m': .03, 'stop': {'result': 'accepted' if uncertain else 'ok'}}
    with use_context(n.context):
        result = await push.push_cargo('盒子', '蓝标', distance_m=.2)
    assert result['result'] == ('accepted' if uncertain else 'blocked')
    assert result['directional_push_attempted'] is False and result['delivered'] is False
    assert result['movement']['distance_m'] == .03 and n.observe.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('feedback', ['standing', 'still_moving', 'lying'])
async def test_cargo_observation_waits_for_delayed_posture_without_skipping_real_feedback(monkeypatch, feedback):
    clock = [100.]
    robot = SimpleNamespace(body_state='moving', is_connected=True,
        _sport_state_observed_at=100., _odom_session_id='odom-test', body_ready_reason=lambda: None)
    monkeypatch.setattr(perception, 'go2', robot)
    monkeypatch.setattr(perception.visual_target, 'go2', robot)
    monkeypatch.setattr(perception.time, 'monotonic', lambda: clock[0])
    async def tick(_delay):
        clock[0] += .1
        robot.body_state = 'moving' if feedback == 'still_moving' else feedback
        robot._sport_state_observed_at = clock[0]
    monkeypatch.setattr(perception.visual_target.asyncio, 'sleep', tick)
    capture = AsyncMock(side_effect=TargetUnlocalized('capture_reached'))
    monkeypatch.setattr(perception.visual_target, '_capture_still', capture)
    with pytest.raises(TargetUnlocalized, match='capture_reached' if feedback == 'standing' else 'posture_not_confirmed'):
        await perception.observe_cargo('盒子')
    assert capture.await_count == int(feedback == 'standing')
