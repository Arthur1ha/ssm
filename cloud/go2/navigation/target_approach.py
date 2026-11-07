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
    """Choose a short mapped observation move; never infer target distance.

    Image bearing is only a direction proposal. Every sampled body pose and
    the space beyond the proposed endpoint must be measured free. Navigator
    independently checks the live map while executing.
    """
    offset, height = visual_target.target_offset(observation['observation'], observation['frame'])
    box = observation['observation']['bbox_1000']
    if height >= .30 or box[3] >= 850:
        raise TargetUnlocalized('object_returns_unavailable')
    # Reuse the previous visual-servo direction convention. This nominal
    # bearing does not become a metric object coordinate or arrival evidence.
    bearing = pose['heading'] - math.atan(offset)
    # A small forward observation can improve depth without rotating through
    # unobserved floor. It is still accepted only by the same full-path check.
    turn = getattr(grid, 'turn_sweep_details', None)
    candidates = []
    if (abs(math.atan(offset)) <= .02
            or (callable(turn) and turn(pose['x'], pose['y'], pose['heading'], bearing)['clear'])):
        candidates.append(bearing)
    if pose['heading'] not in candidates:
        candidates.append(pose['heading'])
    distance = REOBSERVE_STEP_M
    from cloud.go2.navigation.occupancy_grid import TURN_CLEARANCE_M
    reason = 'no_known_free_observation_path'
    for bearing in candidates:
        for fraction in (0., .25, .5, .75, 1., 1.5, 2.):
            x = pose['x'] + distance * fraction * math.cos(bearing)
            y = pose['y'] + distance * fraction * math.sin(bearing)
            if bounds is not None and not (bounds[0] + TURN_CLEARANCE_M <= x <= bounds[2] - TURN_CLEARANCE_M
                                          and bounds[1] + TURN_CLEARANCE_M <= y <= bounds[3] - TURN_CLEARANCE_M):
                reason = 'target_outside_search_bounds'
                break
            if not grid.is_body_clear(x, y, bearing):
                reason = 'no_known_free_observation_path'
                break
        else:
            return {'x': pose['x'] + distance * math.cos(bearing),
                    'y': pose['y'] + distance * math.sin(bearing),
                    'heading': bearing}
    raise TargetUnlocalized(reason)


async def _localize_with_reobservation(target, locate, context, bounds, receipts):
    """Restore bounded visual approach when a tiny target has no depth yet."""
    after_move = None
    for attempt in range(REOBSERVE_MAX_STEPS + 1):
        try:
            return await _await_authorized(locate(target, **(
                {'after_received_at': after_move} if after_move is not None else {})), context)
        except TargetUnlocalized as exc:
            if exc.reason != 'object_returns_unavailable' or attempt == REOBSERVE_MAX_STEPS:
                raise
        observed = await _await_authorized(visual_target.observe_target(target), context)
        if not observed['found']:
            raise TargetUnlocalized('target_not_visible')
        snapshot, reason = navigator.snapshot()
        if reason:
            raise TargetUnlocalized(reason)
        pose, grid = snapshot
        if pose.get('session_id') != observed['frame']['odom_session_id']:
            raise TargetUnlocalized('odom_session_mismatch')
        visual_target._ensure_stable(observed['frame']['pose_at_capture'], pose)
        point = observation_waypoint(grid, pose, observed, bounds)
        emit_agent_event('vision.motion', phase='request', kind='observation_reposition',
                         waypoint=point, step_m=REOBSERVE_STEP_M,
                         target_distance_known=False)
        receipt = await _await_authorized(navigator.go_to_coordinate(
            point['x'], point['y'], owner_session_id=context.session_id,
            # Align then move along the body-forward axis. Splitting a small
            # joystick magnitude diagonally can leave both axes ineffective.
            bounds=bounds, holonomic=False, speed_limit=APPROACH_COMMAND_LIMIT,
            minimum_command_speed=APPROACH_COMMAND_FLOOR,
            arrival_tolerance_m=.08), context, on_result=receipts.append)
        if receipt.get('status') != 'succeeded':
            raise TargetUnlocalized(receipt.get('reason') or 'observation_move_unconfirmed')
        actual = receipt.get('last_trusted_pose') or {}
        if (actual.get('session_id') != pose.get('session_id')
                or not all(type(actual.get(k)) in (int, float) and math.isfinite(actual[k]) for k in ('x', 'y'))
                or math.hypot(actual['x']-pose['x'], actual['y']-pose['y']) < .10):
            raise TargetUnlocalized('observation_move_unconfirmed')
        after_move = time.monotonic()
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


async def navigate_to_visible_target(target: str, *, bounds: tuple | None = None) -> dict:
    """Confirm on different stationary frames, approach, stop and recheck.

    Bounds, when provided by a search, are (min_x,min_y,max_x,max_y) in the
    current odom session. Model profiles alone never establish target depth.
    """
    if not isinstance(target, str) or not target.strip() or len(target) > 100:
        return {'result': 'blocked', 'reason': 'invalid_target'}
    if not go2.is_connected:
        return {'result': 'blocked', 'reason': 'disconnected'}
    context = current_context()
    navigation = None
    location = None
    task_pose = None
    observation_moves = []

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
        location, pose, _ = await _localize_with_reobservation(
            target, locate, context, bounds, observation_moves)
        _authorize(context, renew=True)
        confirmed, pose, _ = await _await_authorized(locate(
            target, after_frame_id=location['source_frame_id'],
            after_received_at=location['source_received_at']), context)
        if not _same_target(location, confirmed):
            raise TargetUnlocalized('target_confirmation_mismatch')
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
                    'observation_moves': observation_moves,
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
                'observation_moves': observation_moves,
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
        if exc.reason in {'authorization_expired', 'control_lease_changed'}:
            stop_result = await navigator.stop_and_confirm(owner_session_id=context.session_id)
        unconfirmed = stop_result is not None and stop_result.get('result') != 'ok'
        if unconfirmed:
            return {'result': 'accepted', 'reason': 'stop_unconfirmed', 'target': target,
                    'cause': exc.reason, 'stop': stop_result, 'navigation': navigation,
                    'observation_moves': observation_moves,
                    'target_location': location, 'last_trusted_pose': task_pose}
        return {'result': 'blocked', 'reason': exc.reason, 'target': target,
                'message': _MESSAGES.get(exc.reason, f'尚未确认到达：{exc.reason}。'),
                'navigation': navigation, 'target_location': location,
                'observation_moves': observation_moves,
                'last_trusted_pose': task_pose}
