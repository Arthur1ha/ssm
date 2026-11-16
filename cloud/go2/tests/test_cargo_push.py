"""Push receipts and delivery separation using fake camera, LiDAR and motion."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cloud.go2.navigation import cargo_push as push
from cloud.go2.perception import cargo as perception
from cloud.go2.perception.visual_target import TargetUnlocalized
from cloud.space.execution import ExecutionContext, use_context


@pytest.fixture
def nudge(monkeypatch, tmp_path):
    monkeypatch.setenv('SSM_EFFECT_LEDGER_DIR', str(tmp_path / 'effects'))
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 'odom-test'}
    samples = []
    for index in range(4):
        samples.append({
            'odom_session_id': 'odom-test', 'frame_id': index + 1,
            'received_at': time.monotonic() + index, 'pose': dict(pose),
            'cargo_location': {'x': .65, 'y': 0., 'uncertainty_m': .1},
            'cargo_bbox_1000': [450, 600, 550, 800],
            'destination_anchor_1000': None, 'near_destination': False,
            'destination_precision': 'approximate_visual_proximity',
            'cluster_extent_xyz_m': [.15, .15, .15], 'cluster_points': 12,
            'voxel_resolution_m': .05})
    for sample in samples[2:]:
        sample['pose']['x'] = .15
        sample['cargo_location']['x'] += .15
    observe = AsyncMock(side_effect=samples)
    move = AsyncMock(return_value={'result': 'ok', 'stop': {'result': 'ok'}})
    grid = SimpleNamespace(is_body_clear=lambda *_: True)
    monkeypatch.setattr(push, 'observe_cargo', observe)
    monkeypatch.setattr(push, '_authorize', lambda *_: None)
    async def authorized(operation, _context):
        return await operation
    monkeypatch.setattr(push, '_await_authorized', authorized)
    monkeypatch.setattr(push.navigator, 'snapshot', lambda: ((dict(pose), grid), None))
    monkeypatch.setattr('cloud.go2.tools.executor.execute_tool', move)
    monkeypatch.setattr(push, '_stop_approach', AsyncMock(return_value={'result': 'ok'}))
    return SimpleNamespace(samples=samples, observe=observe, move=move, grid=grid,
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
    assert nudge.move.await_args.args == ('go2_move', {
        'direction': 'forward', 'distance_m': .15, 'speed': .25})
    assert nudge.move.await_count == 1 and repeated['replayed'] is True
    assert all(call.args[1] is None for call in nudge.observe.await_args_list)


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
async def test_contact_block_still_prevents_all_motion(nudge):
    nudge.grid.is_body_clear = lambda *_: False
    with use_context(nudge.context):
        result = await push.push_cargo('盒子')
    assert result['result'] == 'blocked' and result['reason'] == 'cargo_contact_path_blocked'
    nudge.move.assert_not_awaited()


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
async def test_cargo_and_destination_perception_share_measured_localization(monkeypatch, destination, visible_destination):
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
    def associate(*_args, diagnostics):
        diagnostics['components'] = [{'extent_xyz_m': [.15] * 3, 'points': 12}]
        return {'x': .65, 'y': 0., 'uncertainty_m': .1}
    monkeypatch.setattr(rgb_lidar, 'associate_lidar_target', associate)
    if destination is not None and not visible_destination:
        with pytest.raises(TargetUnlocalized, match='cargo_or_destination_ambiguous'):
            await perception.observe_cargo('深色盒子', destination)
    else:
        result = await perception.observe_cargo('深色盒子', destination)
        assert result['cargo_location']['source'] == 'lidar_foreground_cluster'
        assert result['near_destination'] is visible_destination
        assert ask.await_args.args[-1] == ('cargo_delivery_geometry' if visible_destination else 'cargo_geometry')
    assert geometry.call_count == int(visible_destination)
