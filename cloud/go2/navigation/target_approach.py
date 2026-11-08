"""The single evidence-based target approach executor for tools and search tasks.

Detection and localization live in perception. Every movement is executed by
Navigator with the originating context, then independently reobserved.
"""
from __future__ import annotations

import asyncio
from cloud.agents.common.runner import invoke_blocking
import math
import time

from cloud.go2.connection import go2
from cloud.go2.control import control_lease, LeaseConflict
from cloud.go2.execution import current_context
from cloud.go2.navigation.navigator import navigator
from cloud.go2.navigation.target_view import make_target_hint, select_target_view, target_hint_valid
from cloud.go2.perception import visual_target
from cloud.go2.perception.visual_target import (
    TargetUnlocalized, STANDOFF_M, STANDOFF_TOLERANCE_M, TARGET_KEEPOUT_M,
)
from cloud.observability import emit_agent_event

_MESSAGES = {
    'object_returns_unavailable': '已看到目标，但当前雷达尚不能把它与背景可靠分开，暂未确认到位。',
    'target_lidar_disagreement': '地面接触点与雷达物体簇不一致，暂不能可靠定位目标。',
    'authorization_expired': '本次任务的控制授权已到期。',
    'arrival_target_mismatch': '到位后未能重新确认同一目标。',
}

REOBSERVE_STEP_M = .30
REOBSERVE_MAX_STEPS = 3
# Remote transport uses normalized joystick values, not a calibrated m/s API.
# Site logs show .15 produced no motion, while .242-.30 produced displacement.
APPROACH_COMMAND_LIMIT = .30
APPROACH_COMMAND_FLOOR = .25


def observation_waypoint(grid, pose, observation, bounds=None):
    """Compatibility entrypoint for the shared bearing-only view selector."""
    hint = make_target_hint(observation)
    point = select_target_view(grid, pose, hint, bounds)
    if point is None:
        raise TargetUnlocalized('no_known_free_observation_path')
    return point


async def _localize_with_reobservation(target, locate, context, bounds, receipts, hint_state,
                                     initial_reason=None):
    """Keep recent target direction through temporary misses and missing depth."""
    after_move = hint_state.get('after_move')
    attempted = hint_state.setdefault('attempted', [])
    soft = {'object_returns_unavailable', 'target_not_visible'}
    remaining = max(0, REOBSERVE_MAX_STEPS-len(receipts))
    for attempt in range(remaining + 1):
        try:
            if initial_reason is not None:
                reason, initial_reason = initial_reason, None
                raise TargetUnlocalized(reason)
            return await _await_authorized(locate(target, **(
                {'after_received_at': after_move} if after_move is not None else {})), context)
        except TargetUnlocalized as exc:
            if exc.reason not in soft or attempt == remaining:
                raise
            last_reason = exc.reason
        observed = await _await_authorized(visual_target.observe_target(
            target, **({'after_received_at': after_move} if after_move is not None else {})), context)
        snapshot, reason = navigator.snapshot()
        if reason:
            raise TargetUnlocalized(reason)
        pose, grid = snapshot
        frame = observed.get('frame') or {}
        if pose.get('session_id') != frame.get('odom_session_id'):
            raise TargetUnlocalized('odom_session_mismatch')
        visual_target._ensure_stable(frame['pose_at_capture'], pose)
        if observed.get('found'):
            fresh_hint = make_target_hint(observed)
            if fresh_hint is None:
                raise TargetUnlocalized('vision_result_invalid')
            hint_state['hint'] = fresh_hint
        hint = hint_state.get('hint')
        if not target_hint_valid(hint, pose):
            raise TargetUnlocalized('target_hint_unavailable' if hint else last_reason)
        if not attempted:
            attempted.extend([hint['capture_pose'].copy(), pose.copy()])
        point = select_target_view(grid, pose, hint, bounds, attempted)
        if point is None:
            raise TargetUnlocalized('no_known_free_observation_path')
        attempted.append(point)
        emit_agent_event('vision.motion', phase='request', kind='target_reobservation',
                         waypoint=point, step_m=REOBSERVE_STEP_M,
                         target_distance_known=False)
        receipt = await _await_authorized(navigator.go_to_coordinate(
            point['x'], point['y'], owner_session_id=context.session_id,
            heading=point['heading'], bounds=bounds, holonomic=point['holonomic'],
            speed_limit=APPROACH_COMMAND_LIMIT, minimum_command_speed=APPROACH_COMMAND_FLOOR,
            arrival_tolerance_m=.08), context, on_result=receipts.append)
        if receipt.get('status') != 'succeeded':
            raise TargetUnlocalized(receipt.get('reason') or 'observation_move_unconfirmed')
        actual = receipt.get('last_trusted_pose') or {}
        if (actual.get('session_id') != pose.get('session_id')
                or not all(type(actual.get(k)) in (int, float) and math.isfinite(actual[k])
                           for k in ('x', 'y', 'heading'))):
            raise TargetUnlocalized('observation_move_unconfirmed')
        moved = math.hypot(actual['x']-pose['x'], actual['y']-pose['y'])
        turned = abs(math.atan2(math.sin(actual['heading']-pose['heading']),
                               math.cos(actual['heading']-pose['heading'])))
        if (turned < .10 if point['view_kind'] == 'turn' else moved < .10):
            raise TargetUnlocalized('observation_move_unconfirmed')
        attempted.append(actual.copy())
        after_move = time.monotonic()
        hint_state['after_move'] = after_move
    raise TargetUnlocalized('object_returns_unavailable')


def _authorize(context, *, renew=False):
    if context.expires_at and time.time() >= context.expires_at:
        raise TargetUnlocalized('authorization_expired')
    try:
        if renew:
            control_lease.heartbeat(context)
        else:
            control_lease.acquire(context)
    except LeaseConflict as exc:
        raise TargetUnlocalized('control_lease_changed') from exc


async def _await_authorized(operation, context, *, on_result=None):
    """Keep the original credential/lease valid throughout a device operation."""
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            _authorize(context, renew=True)
            await asyncio.wait({task}, timeout=0.2)
        _authorize(context, renew=True)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A completed movement remains evidence even if authorization expires
        # at completion, or cancellation returns a terminal navigation receipt.
        if on_result is not None and not task.cancelled() and task.exception() is None:
            on_result(task.result())


def _same_target(first: dict, second: dict) -> bool:
    return (first['odom_session_id'] == second['odom_session_id']
            and first['source_frame_id'] != second['source_frame_id']
            and second['source_received_at'] > first['source_received_at']
            and math.hypot(first['x'] - second['x'], first['y'] - second['y']) <= 0.35)


async def navigate_to_visible_target(target: str, *, bounds: tuple | None = None,
                                     target_hint: dict | None = None) -> dict:
    """Confirm on different stationary frames, approach, stop and recheck.

    Bounds, when provided by a search, are (min_x,min_y,max_x,max_y) in the
    current odom session. Model profiles alone never establish target depth.
    """
    if not isinstance(target, str) or not target.strip() or len(target) > 100:
        return {'result': 'blocked', 'reason': 'invalid_target', 'target_hint': target_hint,
                'observation_moves': []}
    if not go2.is_connected:
        return {'result': 'blocked', 'reason': 'disconnected', 'target_hint': target_hint,
                'observation_moves': []}
    context = current_context()
    navigation = None
    location = None
    task_pose = None
    observation_moves = []
    hint_state = {'hint': target_hint}

    def remember_navigation(receipt):
        nonlocal navigation, task_pose
        navigation = receipt
        task_pose = receipt.get('last_trusted_pose') or task_pose

    try:
        if getattr(go2, 'fsm_state', None) != 'standing':
            raise TargetUnlocalized('posture_not_confirmed')
        try:
            calibration = visual_target.load_calibration(serial=getattr(go2, '_connected_serial', None))
        except TargetUnlocalized as exc:
            if exc.reason != 'calibration_unavailable':
                raise
            calibration = None
        async def locate(name, **kwargs):
            if calibration is not None:
                return await visual_target.locate_ground_target(name, calibration, **kwargs)
            return await visual_target.locate_lidar_target(name, **kwargs)
        _authorize(context)
        retry_reason = None
        while True:
            location, pose, _ = await _localize_with_reobservation(
                target, locate, context, bounds, observation_moves, hint_state, retry_reason)
            _authorize(context, renew=True)
            try:
                confirmed, pose, _ = await _await_authorized(locate(
                    target, after_frame_id=location['source_frame_id'],
                    after_received_at=location['source_received_at']), context)
            except TargetUnlocalized as exc:
                if (exc.reason not in {'object_returns_unavailable', 'target_not_visible'}
                        or len(observation_moves) >= REOBSERVE_MAX_STEPS):
                    raise
                retry_reason = exc.reason
                continue
            if not _same_target(location, confirmed):
                raise TargetUnlocalized('target_confirmation_mismatch')
            break
        location = confirmed
        task_pose = pose
        snapshot, reason = navigator.snapshot()
        if reason:
            raise TargetUnlocalized(reason)
        current_pose, grid = snapshot
        if current_pose.get('session_id') != location['odom_session_id']:
            raise TargetUnlocalized('odom_session_mismatch')
        stop = await _await_authorized(invoke_blocking(
            visual_target.select_standoff, grid, current_pose, location), context)
        if bounds is not None:
            x0, y0, x1, y1 = bounds
            if not (x0 <= stop['x'] <= x1 and y0 <= stop['y'] <= y1
                    and x0 <= location['x'] <= x1 and y0 <= location['y'] <= y1):
                raise TargetUnlocalized('target_outside_search_bounds')
        _authorize(context, renew=True)
        emit_agent_event('vision.motion', phase='request', kind='target_approach',
                         target_location=location, stop=stop)
        options = dict(owner_session_id=context.session_id, heading=stop['heading'],
                       arrival_tolerance_m=.10,
                       keepout=(location['x'], location['y'], TARGET_KEEPOUT_M + location['uncertainty_m']))
        if calibration is None:
            # Recover the existing holonomic, target-facing map follower.
            options.update(holonomic=True, face_target=(location['x'], location['y']),
                           speed_limit=APPROACH_COMMAND_LIMIT,
                           minimum_command_speed=APPROACH_COMMAND_FLOOR)
        if bounds is not None:
            options['bounds'] = bounds
        navigation = await _await_authorized(
            navigator.go_to_coordinate(stop['x'], stop['y'], **options), context,
            on_result=remember_navigation)
        task_pose = navigation.get('last_trusted_pose') or task_pose
        if navigation.get('status') != 'succeeded':
            return {'result': 'error' if navigation.get('status') == 'failed' else navigation.get('status', 'blocked'),
                    'reason': navigation.get('reason'), 'target': target,
                    'navigation': navigation, 'target_location': location,
                    'observation_moves': observation_moves, 'target_hint': hint_state.get('hint'),
                    'last_trusted_pose': task_pose}
        stopped_at = time.monotonic()
        _authorize(context, renew=True)
        verified, pose, _ = await _await_authorized(locate(
            target, after_frame_id=location['source_frame_id'],
            after_received_at=stopped_at), context)
        if not _same_target(location, verified):
            raise TargetUnlocalized('arrival_target_mismatch')
        distance = math.hypot(pose['x'] - verified['x'], pose['y'] - verified['y'])
        if not STANDOFF_M - STANDOFF_TOLERANCE_M <= distance <= STANDOFF_M + STANDOFF_TOLERANCE_M:
            raise TargetUnlocalized('arrival_standoff_mismatch')
        bearing = math.atan2(verified['y'] - pose['y'], verified['x'] - pose['x'])
        if abs(math.atan2(math.sin(bearing - pose['heading']), math.cos(bearing - pose['heading']))) > 0.30:
            raise TargetUnlocalized('arrival_heading_mismatch')
        _authorize(context, renew=True)
        return {'result': 'ok', 'target': target, 'confirmation': 'odom_stopped_and_visual_recheck',
                'target_location': verified, 'navigation': navigation, 'stop': stop,
                'observation_moves': observation_moves, 'target_hint': hint_state.get('hint'),
                'distance_to_target_m': round(distance, 3),
                'front_clearance_to_contact_m': round(distance - visual_target.ROBOT_HALF_LENGTH_M, 3),
                'message': f'已到「{target}」附近，停稳后已重新确认目标。'}
    except asyncio.CancelledError:
        await navigator.stop_and_confirm(owner_session_id=context.session_id)
        raise
    except TargetUnlocalized as exc:
        if observation_moves and task_pose is None:
            task_pose = observation_moves[-1].get('last_trusted_pose')
        stop_result = None
        if exc.reason in {'authorization_expired', 'control_lease_changed',
                          'odom_session_mismatch', 'map_session_changed', 'map_mismatch',
                          'disconnected', 'stop_unconfirmed', 'arrival_unconfirmed'}:
            stop_result = await navigator.stop_and_confirm(owner_session_id=context.session_id)
        unconfirmed = stop_result is not None and stop_result.get('result') != 'ok'
        if unconfirmed:
            return {'result': 'accepted', 'reason': 'stop_unconfirmed', 'target': target,
                    'cause': exc.reason, 'stop': stop_result, 'navigation': navigation,
                    'observation_moves': observation_moves, 'target_hint': hint_state.get('hint'),
                    'target_location': location, 'last_trusted_pose': task_pose}
        return {'result': 'blocked', 'reason': exc.reason, 'target': target,
                'message': _MESSAGES.get(exc.reason, f'尚未确认到达：{exc.reason}。'),
                'navigation': navigation, 'target_location': location,
                'observation_moves': observation_moves, 'target_hint': hint_state.get('hint'),
                'last_trusted_pose': task_pose}
