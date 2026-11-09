"""Target confirmation and safety regressions; only fake sensor/navigation I/O."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from cloud.go2.navigation import target_approach as approach
from cloud.go2.perception import visual_target as vision
from cloud.go2.perception.rgb_lidar import target_cluster
from cloud.space.execution import ExecutionContext


def test_background_returns_never_become_target_median():
    background = np.array([[4, y, .3] for y in np.linspace(-1, 1, 30)])
    foreground = np.array([[1, -.04, .2], [1.03, 0, .22], [.97, .04, .24]])
    cluster = target_cluster(np.vstack([background, foreground]), (1, 0), .05)
    assert cluster['points'] == 3
    assert np.linalg.norm(np.array(cluster['xy']) - (1, 0)) < .05
    assert target_cluster(background, (1, 0), .05) is None


def test_disjoint_object_clusters_are_ambiguous():
    points = np.array([[1, -.22, .2], [1.03, -.24, .22], [.97, -.21, .24],
                       [1, .22, .2], [1.03, .24, .22], [.97, .21, .24]])
    assert target_cluster(points, (1, 0), .1) is None


def setup_approach(monkeypatch, *, nav_status='succeeded', final_shift=0):
    monkeypatch.setattr(approach, 'go2', SimpleNamespace(is_connected=True,
                        body_state='standing', _connected_serial='robot'))
    context = ExecutionContext('visitor', 'visitor', False, expires_at=int(time.time()+120))
    monkeypatch.setattr(approach, 'current_context', lambda: context)
    monkeypatch.setattr(approach, 'control_lease', SimpleNamespace(acquire=lambda _: None, heartbeat=lambda _: None))
    monkeypatch.setattr(vision, 'load_calibration', lambda **_: {})
    calls = []
    pose = {'x': .45, 'y': 0., 'heading': 0., 'session_id': 's'}

    async def locate(target, calibration, **kwargs):
        calls.append(kwargs)
        n = len(calls)
        return ({'x': 2 + (final_shift if n == 3 else 0), 'y': 0., 'uncertainty_m': .05,
                 'odom_session_id': 's', 'source_frame_id': n,
                 'source_received_at': time.monotonic() + n, 'frame_id': 'odom'}, pose, object())

    monkeypatch.setattr(vision, 'locate_ground_target', locate)
    monkeypatch.setattr(vision, 'select_standoff', lambda *_: {'x': .45, 'y': 0., 'heading': 0.})
    navigate = AsyncMock(return_value={'status': nav_status, 'task_id': 'nav-1', 'last_trusted_pose': pose})
    monkeypatch.setattr(approach, 'navigator', SimpleNamespace(
        snapshot=lambda: ((pose, object()), None), go_to_coordinate=navigate,
        stop_and_confirm=AsyncMock(return_value={'result': 'ok'})))
    return calls, navigate


@pytest.mark.asyncio
async def test_confirmation_navigation_and_post_stop_recheck(monkeypatch):
    calls, navigate = setup_approach(monkeypatch)
    result = await approach.navigate_to_visible_target('owl', bounds=(-1, -1, 3, 1))
    assert result['result'] == 'ok'
    assert result['confirmation'] == 'odom_stopped_and_visual_recheck'
    assert len(calls) == 3
    assert calls[1]['after_frame_id'] == 1
    assert calls[2]['after_frame_id'] == 2
    assert calls[2]['after_received_at'] <= time.monotonic()
    assert navigate.call_args.kwargs['owner_session_id'] == 'visitor'
    assert navigate.call_args.kwargs['bounds'] == (-1, -1, 3, 1)


@pytest.mark.asyncio
async def test_failed_navigation_keeps_found_evidence_without_success(monkeypatch):
    calls, _ = setup_approach(monkeypatch, nav_status='blocked')
    result = await approach.navigate_to_visible_target('owl')
    assert result['result'] == 'blocked'
    assert result['target_location']['source_frame_id'] == 2
    assert len(calls) == 2
    assert 'confirmation' not in result


@pytest.mark.asyncio
async def test_different_final_object_cannot_confirm_arrival(monkeypatch):
    setup_approach(monkeypatch, final_shift=1)
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'arrival_target_mismatch'
    assert result['result'] == 'blocked'


@pytest.mark.asyncio
async def test_same_frame_cannot_confirm_target(monkeypatch):
    _, navigate = setup_approach(monkeypatch)
    location = {'x': 2., 'y': 0., 'odom_session_id': 's', 'source_frame_id': 1, 'source_received_at': 1.}
    monkeypatch.setattr(vision, 'locate_ground_target', AsyncMock(return_value=(location, {}, object())))
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'target_confirmation_mismatch'
    navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_original_credentials_never_move(monkeypatch):
    _, navigate = setup_approach(monkeypatch)
    monkeypatch.setattr(approach, 'current_context', lambda: ExecutionContext('visitor', 'visitor', False, expires_at=1))
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'authorization_expired'
    navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_out_of_bounds_target_does_not_navigate(monkeypatch):
    _, navigate = setup_approach(monkeypatch)
    result = await approach.navigate_to_visible_target('owl', bounds=(-1,-1,1,1))
    assert result['reason'] == 'target_outside_search_bounds'
    navigate.assert_not_awaited()


@pytest.mark.parametrize('field,value,reason', [
    ('received_at', -10, 'frame_stale'),
    ('odom_session_id', 'old', 'video_session_mismatch'),
])
def test_stale_or_old_session_frame_cannot_localize(field, value, reason):
    now = time.monotonic()
    frame = {'received_at': now, 'odom_session_id': 's', field: value}
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    with pytest.raises(vision.TargetUnlocalized, match=reason):
        vision.validate_samples(frame, pose, now, now, 'odom', 'odom', 's')


def test_bad_sample_timing_is_rejected():
    now = time.monotonic()
    frame = {'received_at': now, 'odom_session_id': 's'}
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    with pytest.raises(vision.TargetUnlocalized, match='sensor_frame_skew'):
        vision.validate_samples(frame, pose, now, now-1, 'odom', 'odom', 's')


@pytest.mark.asyncio
async def test_exploration_describes_scene_without_selecting_a_search_target(monkeypatch):
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    frame = {'pose_at_capture': pose, 'odom_session_id': 's', 'frame_id': 2, 'received_at': 2.}
    monkeypatch.setattr(vision, '_capture_still', AsyncMock(return_value=(b'jpeg', frame, 1.)))
    detect = AsyncMock(side_effect=AssertionError('no model needed'))
    monkeypatch.setattr(vision, 'detect_visible_target', detect)
    describe = AsyncMock(return_value={'scene_summary': '桌子旁有红色盒子。'})
    monkeypatch.setattr(vision, '_ask_vision', describe)
    monkeypatch.setattr(vision, '_live_pose', lambda _: pose)
    monkeypatch.setattr(vision, 'save_diagnostic_frame', lambda *_: None)
    from cloud.go2 import activity_media
    monkeypatch.setattr(activity_media, 'save_temporary_snapshot', lambda *_: '/image')
    result = await vision.observe_target('')
    assert result['found'] is False and result['quality'] == 1
    assert 'jpeg' not in result
    assert result['observation']['scene_summary'] == '桌子旁有红色盒子。'
    assert describe.await_args.args[-1] == 'scene'
    detect.assert_not_awaited()


def capture_robot(monkeypatch, *, cached=False, changing=False):
    tick = [10.]
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}

    class Robot:
        is_connected = True
        _odom_session_id = 's'
        odom_frame_id = 'odom'

        @property
        def odom_received_at(self):
            return tick[0]

        @property
        def odom(self):
            return {**pose, 'x': tick[0]-10 if changing else 0.}

        def latest_frame_snapshot(self):
            return b'jpeg', {'frame_id': 10 if cached else int(tick[0]*100),
                             'received_at': 10. if cached else tick[0],
                             'pose_received_at': 10. if cached else tick[0],
                             'pose_at_capture': self.odom, 'odom_session_id': 's',
                             'width': 640, 'height': 360}

    async def sleep(_):
        tick[0] += .05

    monkeypatch.setattr(vision, 'time', SimpleNamespace(monotonic=lambda: tick[0], time=time.time))
    monkeypatch.setattr(vision, 'asyncio', SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(vision, 'invoke_blocking', AsyncMock(return_value=1.))
    monkeypatch.setattr(vision, 'go2', Robot())
    return tick


@pytest.mark.asyncio
async def test_capture_waits_for_stillness_and_post_move_frame(monkeypatch):
    capture_robot(monkeypatch)
    _, frame, quality = await vision._capture_still(after_received_at=10.4)
    assert frame['received_at'] > 10.4
    assert quality == 1.


@pytest.mark.asyncio
async def test_cached_frame_does_not_fake_second_observation(monkeypatch):
    capture_robot(monkeypatch, cached=True)
    with pytest.raises(vision.TargetUnlocalized, match='fresh_still_frame_unavailable'):
        await vision._capture_still(after_frame_id=10)


@pytest.mark.asyncio
async def test_moving_robot_cannot_mark_camera_coverage(monkeypatch):
    capture_robot(monkeypatch, changing=True)
    with pytest.raises(vision.TargetUnlocalized, match='pose_changed_during_detection'):
        await vision._capture_still()


def use_lidar_without_site_calibration(monkeypatch):
    def absent(**kwargs):
        raise vision.TargetUnlocalized('calibration_unavailable')
    monkeypatch.setattr(vision, 'load_calibration', absent)
    async def locate(target, **kwargs):
        return await vision.locate_ground_target(target, {}, **kwargs)
    monkeypatch.setattr(vision, 'locate_lidar_target', locate, raising=False)


@pytest.mark.asyncio
async def test_missing_site_file_uses_lidar_evidence_and_keeps_final_recheck(monkeypatch):
    calls, navigate = setup_approach(monkeypatch)
    use_lidar_without_site_calibration(monkeypatch)
    result = await approach.navigate_to_visible_target('white floor owl')
    assert result['result'] == 'ok'
    assert len(calls) == 3
    assert navigate.call_args.kwargs['holonomic'] is True
    assert navigate.call_args.kwargs['face_target'] == (2, 0.)
    assert result['confirmation'] == 'odom_stopped_and_visual_recheck'


def setup_observation_move(monkeypatch, *, clear=True):
    calls, navigate = setup_approach(monkeypatch)
    use_lidar_without_site_calibration(monkeypatch)
    pose = {'x': .45, 'y': 0., 'heading': 0., 'session_id': 's'}
    grid = SimpleNamespace(is_body_clear=lambda x, y, h: clear and abs(y) < .01,
                           turn_sweep_details=lambda x, y, h, end: {'clear': clear and abs(y) < .01 and abs(end) < .01})
    navigate.return_value = {'status': 'succeeded', 'last_trusted_pose': {**pose, 'x': .75}}
    monkeypatch.setattr(approach.navigator, 'snapshot', lambda: ((pose, grid), None))
    monkeypatch.setattr(vision, 'observe_target', AsyncMock(return_value={
        'found': True, 'frame': {'width': 640, 'height': 360, 'odom_session_id': 's', 'pose_at_capture': pose,
                                'frame_id': 1, 'received_at': time.monotonic()},
        'observation': {'found': True, 'bbox_1000': [450, 400, 550, 600]}}))
    return calls, navigate


@pytest.mark.asyncio
async def test_short_map_move_can_reacquire_depth_but_cannot_replace_arrival_evidence(monkeypatch):
    calls, navigate = setup_observation_move(monkeypatch)
    original = vision.locate_lidar_target
    attempts = []
    async def locate(target, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise vision.TargetUnlocalized('object_returns_unavailable')
        return await original(target, **kwargs)
    monkeypatch.setattr(vision, 'locate_lidar_target', locate)
    result = await approach.navigate_to_visible_target('owl')
    assert result['result'] == 'ok'
    assert len(result['observation_moves']) == 1
    assert navigate.await_count == 2
    assert navigate.await_args_list[0].args == (.75, 0.)
    assert len(calls) == 3  # initial metric estimate, confirmation, arrival recheck


@pytest.mark.asyncio
async def test_reobservation_has_finite_motion_and_never_claims_arrival_without_depth(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch)
    monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
        side_effect=vision.TargetUnlocalized('object_returns_unavailable')))
    result = await approach.navigate_to_visible_target('owl')
    assert result['result'] == 'blocked'
    # A mocked unchanged live pose cannot select the same viewpoint repeatedly.
    assert result['reason'] == 'no_known_free_observation_path'
    assert navigate.await_count == 1
    assert len(result['observation_moves']) == 1
    assert 'confirmation' not in result


@pytest.mark.asyncio
async def test_missing_depth_never_bypasses_measured_free_path(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch, clear=False)
    monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
        side_effect=vision.TargetUnlocalized('object_returns_unavailable')))
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'no_known_free_observation_path'
    navigate.assert_not_awaited()


def test_observation_can_translate_forward_without_unverified_rotation():
    pose = {'x': 0., 'y': 0., 'heading': 0., 'session_id': 's'}
    observed = {'found': True, 'frame': {'width': 640, 'height': 360, 'pose_at_capture': pose,
                                        'odom_session_id': 's', 'frame_id': 1, 'received_at': time.monotonic()},
                'observation': {'found': True, 'bbox_1000': [870, 520, 895, 565]}}
    grid = SimpleNamespace(is_body_clear=lambda x, y, heading: abs(y) < .01,
                           turn_sweep_details=lambda x, y, h, end: {'clear': abs(y) < .01 and abs(end) < .01})
    point = approach.select_target_view(grid, pose, approach.make_target_hint(observed))
    assert (point['x'], point['y'], point['heading']) == (.30, 0., 0.)
    grid.is_body_clear = lambda *_: False
    assert approach.select_target_view(grid, pose, approach.make_target_hint(observed)) is None


@pytest.mark.asyncio
async def test_stationary_navigation_receipt_cannot_count_as_observation_move(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch)
    navigate.return_value['last_trusted_pose']['x'] = .45
    monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
        side_effect=vision.TargetUnlocalized('object_returns_unavailable')))
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'observation_move_unconfirmed'
    assert navigate.await_count == 1
    assert result['last_trusted_pose']['x'] == .45


@pytest.mark.asyncio
async def test_pose_changed_after_observation_blocks_reposition(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch)
    vision.observe_target.return_value['frame']['pose_at_capture'] = {
        'x': 1., 'y': 0., 'heading': 0., 'session_id': 's'}
    monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
        side_effect=vision.TargetUnlocalized('object_returns_unavailable')))
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'pose_changed_during_detection'
    navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporary_visual_miss_keeps_hint_then_requires_metric_confirmation(monkeypatch):
    calls, navigate = setup_observation_move(monkeypatch)
    hint = approach.make_target_hint(vision.observe_target.return_value)
    vision.observe_target.return_value['found'] = False
    original = vision.locate_lidar_target
    attempts = []
    async def locate(target, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise vision.TargetUnlocalized('target_not_visible')
        return await original(target, **kwargs)
    monkeypatch.setattr(vision, 'locate_lidar_target', locate)
    result = await approach.navigate_to_visible_target('owl', target_hint=hint)
    assert result['result'] == 'ok'
    assert result['target_hint'] == hint
    assert len(result['observation_moves']) == 1
    assert 'after_received_at' in attempts[1]
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_expired_hint_and_visual_miss_cannot_move(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch)
    hint = approach.make_target_hint(vision.observe_target.return_value)
    hint['received_at'] -= 61
    vision.observe_target.return_value['found'] = False
    monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
        side_effect=vision.TargetUnlocalized('target_not_visible')))
    result = await approach.navigate_to_visible_target('owl', target_hint=hint)
    assert result['reason'] == 'target_hint_unavailable'
    assert result['target_hint'] == hint
    navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_metric_confirmation_miss_can_reobserve_with_same_budget(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch)
    original = vision.locate_lidar_target
    count = 0
    async def locate(target, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise vision.TargetUnlocalized('target_not_visible')
        return await original(target, **kwargs)
    monkeypatch.setattr(vision, 'locate_lidar_target', locate)
    result = await approach.navigate_to_visible_target('owl')
    assert result['result'] == 'ok'
    assert len(result['observation_moves']) == 1
    assert navigate.await_count == 2
    assert result['confirmation'] == 'odom_stopped_and_visual_recheck'


@pytest.mark.asyncio
async def test_rotation_view_requires_real_turn_not_translation(monkeypatch):
    _, navigate = setup_observation_move(monkeypatch)
    sample = vision.observe_target.return_value
    pose = sample['frame']['pose_at_capture']
    g = SimpleNamespace(is_body_clear=lambda x,y,h: abs(x-pose['x']) < .01 and abs(y) < .01,
                        turn_sweep_details=lambda *args: {'clear': True})
    monkeypatch.setattr(approach.navigator, 'snapshot', lambda: ((pose,g),None))
    navigate.return_value = {'status': 'succeeded', 'last_trusted_pose': pose.copy()}
    monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
        side_effect=vision.TargetUnlocalized('object_returns_unavailable')))
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'observation_move_unconfirmed'
    assert navigate.call_args.args == (pose['x'], pose['y'])
    assert abs(navigate.call_args.kwargs['heading']) >= .30


@pytest.mark.asyncio
@pytest.mark.parametrize('observation', [False, True])
async def test_completed_movement_receipt_survives_authorization_expiry(monkeypatch, observation):
    if observation:
        _, navigate = setup_observation_move(monkeypatch)
        monkeypatch.setattr(vision, 'locate_lidar_target', AsyncMock(
            side_effect=vision.TargetUnlocalized('object_returns_unavailable')))
    else:
        _, navigate = setup_approach(monkeypatch)
    completed = [False]
    authorize = approach._authorize
    def expires_after_motion(context, **kwargs):
        if completed[0]:
            raise vision.TargetUnlocalized('authorization_expired')
        authorize(context, **kwargs)
    async def move(*args, **kwargs):
        completed[0] = True
        return navigate.return_value
    navigate.side_effect = move
    monkeypatch.setattr(approach, '_authorize', expires_after_motion)
    result = await approach.navigate_to_visible_target('owl')
    assert result['reason'] == 'authorization_expired'
    assert result['result'] == 'blocked'
    receipt = result['observation_moves'][0] if observation else result['navigation']
    assert receipt == navigate.return_value
    assert result['last_trusted_pose'] == receipt['last_trusted_pose']
    navigate.assert_awaited_once()
    approach.navigator.stop_and_confirm.assert_awaited_once()
