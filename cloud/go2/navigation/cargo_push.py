"""Guarded cargo nudges and coarse directional delivery using ordinary Go2 gait.

The shared obstacle map stays intact. A bounded contact step permits only the
freshly re-associated cargo component; other obstacles remain blocked. Progress
Directional attempts require real movement and stopping; measured cargo motion
and delivery remain distinct facts.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import math
import time
import uuid

from cloud.agents.common.effects import FileEffectLedger, current_effect, use_effect
from cloud.agents.common.runner import invoke_blocking
from cloud.go2.control import control_lease
from cloud.go2.navigation.navigator import navigator
from cloud.go2.navigation.cargo_contact import CargoContact, permit_contact
from cloud.go2.navigation.occupancy_grid import ROBOT_HALF_LENGTH_M, SAFETY_MARGIN_M
from cloud.go2.navigation.target_approach import _authorize, _await_authorized, _stop_approach
from cloud.go2.perception.cargo import observe_cargo
from cloud.go2.perception.visual_target import TargetUnlocalized, _ensure_stable
from cloud.space.execution import current_context

_reference: ContextVar[dict | None] = ContextVar('cargo_push_reference', default=None)
_request: ContextVar[str | None] = ContextVar('cargo_push_request', default=None)
_signal: ContextVar[tuple | None] = ContextVar('cargo_push_signal', default=None)

_REASONS = {
    'cargo_target_mismatch': '候选货物尚未与用户所指的物体核对一致，未执行移动或推送；需要重新辨认，不能改推远处同类物体。',
    'cargo_alignment_required': '货物尚未在机身正前方的可推送方向，需要先重新对齐。',
    'cargo_destination_alignment_required': '货物与指定配送区域尚未在同一推送方向，需要先重新对齐。',
    'cargo_or_destination_ambiguous': '当前画面尚不能辨认指定货物或目的地在地面的位置。',
    'cargo_ambiguous': '当前画面无法唯一确认指定的地面货物或其地面接触位置。',
    'destination_geometry_invalid': '尚未辨认出指定色块、地面标记或物体标识的位置。',
    'visual_distance_unavailable': '当前画面无法估算货物距离，需要重新观察。',
    'visual_advance_unavailable': '当前画面未提供可靠的建议前进距离，未开始移动。',
    'delivery_direction_unavailable': '当前画面尚不能判断货物到指定目的地的方向，未开始推送。',
    'cargo_returns_unavailable': '雷达尚不能可靠分离这件货物，未开始推送。',
    'cargo_association_unconfirmed': '货物的视觉与雷达对应关系尚未确认。',
    'cargo_contact_geometry_unavailable': '当前测量不足以确认可靠的靠近或腿部接触位置。',
    'cargo_contact_path_blocked': '机身避障检查未通过，靠近或接触货物的通路受阻。',
    'cargo_contact_tracking_lost': '推送中无法继续核对同一货物，已停止。',
    'cargo_contact_scope_changed': '本次接触范围或定位条件已变化，已停止。',
    'cargo_motion_unconfirmed': '尚未测得可靠的货物移动，本次不算推送完成。',
    'cargo_move_unconfirmed': '本次前进或停稳结果尚未确认，需要核对原任务。',
    'cargo_identity_or_position_changed': '货物的位置变化超出本步可确认范围，需要重新核对同一件货物。',
    'cargo_search_reference_unconfirmed': '当前货物尚未与之前找货的实际观察核对一致。',
    'cargo_search_target_mismatch': '当前看到的货物与之前找货的观察位置不一致。',
    'control_lease_changed': '设备控制已由新的请求接管，本次推送已让位。',
    'odom_session_mismatch': '定位会话已变化，本次推送不能继续沿用旧位置。',
    'stop_unconfirmed': '机身停稳尚未确认，需要先核对设备状态。',
    'posture_not_confirmed': '尚未确认机器狗处于可以推送的站立姿态。',
    'measured_floor_unavailable': '当前雷达尚未确认地面测量，未开始推送。',
}


@contextmanager
def expect_cargo(reference: dict | None, *, request_id: str | None = None,
                 signal_rgb: tuple[int, int, int] | None = None):
    """Bind prior tool receipts and a stable game-step ID, outside model input."""
    if reference is not None and not isinstance(reference, dict):
        raise ValueError('invalid_cargo_reference')
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise ValueError('invalid_cargo_request_id')
    if signal_rgb is not None and (not isinstance(signal_rgb, (tuple, list)) or len(signal_rgb) != 3
            or any(type(value) is not int or not 0 <= value <= 255 for value in signal_rgb)):
        raise ValueError('invalid_delivery_signal_rgb')
    token = _reference.set(deepcopy(reference))
    request_token = _request.set(request_id)
    signal_token = _signal.set(tuple(signal_rgb) if signal_rgb is not None else None)
    try:
        yield
    finally:
        _signal.reset(signal_token)
        _request.reset(request_token)
        _reference.reset(token)


def _distance(first, second):
    return math.hypot(first['x'] - second['x'], first['y'] - second['y'])


def _check_after_frame(before, after):
    """Compare different observations across the real, stopped movement."""
    if (before['odom_session_id'] != after['odom_session_id']
            or before['frame_id'] == after['frame_id']
            or after['received_at'] <= before['received_at']):
        raise TargetUnlocalized('cargo_observation_session_changed')


def _stable_cluster_shape(first, second):
    """Changing point association cannot masquerade as a translated cargo."""
    resolution = max(first.get('voxel_resolution_m', .1), second.get('voxel_resolution_m', .1))
    a, b = first.get('cluster_extent_xyz_m'), second.get('cluster_extent_xyz_m')
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != 3 or len(b) != 3:
        return False
    if max(abs(x - y) for x, y in zip(a, b)) > resolution:
        return False
    counts = first.get('cluster_points', 0), second.get('cluster_points', 0)
    return min(counts) >= 6 and max(counts) <= min(counts) * 1.5


def _reference_for(sample, cargo, destination):
    return {'cargo': cargo, 'destination': destination,
            'odom_session_id': sample['odom_session_id'],
            'cargo_location': sample['cargo_location'],
            'destination_kind': sample.get('destination_kind'),
            'observation': {key: value for key, value in sample.items()
                            if key != 'contact_observation'}}


def _check_search_reference(reference, sample):
    """Associate the first metric sample with the actual search receipt."""
    evidence = reference.get('search_evidence') or {}
    observation, pose = evidence.get('observation') or {}, evidence.get('pose') or {}
    stamp = evidence.get('image_time')
    if (observation.get('found') is not True
            or type(stamp) not in (int, float) or not math.isfinite(stamp)
            or not 0 <= time.monotonic() - stamp <= 300
            or pose.get('session_id') != sample['odom_session_id']):
        raise TargetUnlocalized('cargo_search_reference_unconfirmed')
    _ensure_stable(pose, sample['pose'])
    previous, current = observation.get('bbox_1000'), sample['cargo_bbox_1000']
    if (not isinstance(previous, list) or len(previous) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in previous)):
        raise TargetUnlocalized('cargo_search_reference_unconfirmed')
    x0, y0 = max(previous[0], current[0]), max(previous[1], current[1])
    x1, y1 = min(previous[2], current[2]), min(previous[3], current[3])
    overlap = max(0, x1 - x0) * max(0, y1 - y0)
    area = ((previous[2] - previous[0]) * (previous[3] - previous[1])
            + (current[2] - current[0]) * (current[3] - current[1]) - overlap)
    if area <= 0 or overlap / area < .25:
        raise TargetUnlocalized('cargo_search_target_mismatch')


def _aligned(sample):
    pose, target = sample['pose'], sample['cargo_location']
    dx, dy = target['x'] - pose['x'], target['y'] - pose['y']
    forward = dx * math.cos(pose['heading']) + dy * math.sin(pose['heading'])
    lateral = -dx * math.sin(pose['heading']) + dy * math.cos(pose['heading'])
    # One bearing check suffices; pixel centering and a second lateral gate
    # disagree with the measured target as camera perspective changes.
    if forward <= 0 or abs(math.atan2(lateral, forward)) > .3:
        raise TargetUnlocalized('cargo_alignment_required')
    return forward


def _advance_distance(requested, sample):
    value = requested if requested is not None else sample.get('advance_distance_m')
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise TargetUnlocalized('visual_advance_unavailable')
    return float(value)


def _check_forward_path(grid, pose, distance):
    # Sample at map resolution across the requested route, regardless of its
    # length. Checking only six points could miss obstacles on a longer move.
    x = pose['x'] + distance * math.cos(pose['heading'])
    y = pose['y'] + distance * math.sin(pose['heading'])
    if not all(math.isfinite(v) for v in (x, y)) or not grid.is_body_clear(x, y, pose['heading']):
        raise TargetUnlocalized('cargo_contact_path_blocked')
    count = max(1, math.ceil(distance / grid.resolution))
    for i in range(count + 1):
        offset = distance * i / count
        if not grid.is_body_clear(pose['x'] + offset * math.cos(pose['heading']),
                                  pose['y'] + offset * math.sin(pose['heading']), pose['heading']):
            raise TargetUnlocalized('cargo_contact_path_blocked')


async def _visual_step(cargo, destination, distance_m, before, context, ledger, identity):
    """One coarse visual step on the full map; no contact obstacle exemption."""
    from cloud.go2.tools.executor import execute_tool

    snapshot, reason = navigator.snapshot()
    if reason:
        raise TargetUnlocalized(reason)
    pose, grid = snapshot
    if pose.get('session_id') != before['odom_session_id']:
        raise TargetUnlocalized('odom_session_mismatch')
    _ensure_stable(before['pose'], pose)
    target = before['cargo_location']
    dx, dy = target['x'] - pose['x'], target['y'] - pose['y']
    bearing = math.atan2(math.sin(math.atan2(dy, dx) - pose['heading']),
                         math.cos(math.atan2(dy, dx) - pose['heading']))
    turning = abs(bearing) > .12
    advance = distance_m
    close = math.hypot(dx, dy) <= ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M + advance + .15
    if turning:
        # Translation's .25 command is not an angular-speed default. Let the
        # ordinary move adapter resolve the declared turn-axis speed.
        params = {'direction': 'turn_left' if bearing > 0 else 'turn_right',
                  'angle_deg': math.degrees(abs(bearing))}
    else:
        _check_forward_path(grid, pose, advance)
        params = {'direction': 'forward', 'distance_m': advance, 'speed': .25}
    # Ordinary movement retains its preflight, live obstacle checks, control
    # lease, teaching validation, odometry and real stop confirmation.
    with use_effect(ledger, identity):
        movement = await execute_tool('go2_move', params, context=context)
    result = {'delivered': False, 'cargo_before': before, 'cargo_after': None,
              'movement': movement, 'stop': movement.get('stop'),
              'reference': _reference_for(before, cargo, destination),
              'cargo_precision': 'approximate_visual', 'nudge_attempted': close and not turning}
    if movement.get('result') != 'ok' or (movement.get('stop') or {}).get('result') != 'ok':
        uncertain = (movement.get('result') in {'accepted', 'executing', 'timeout', 'unknown'}
                     or (movement.get('stop') or {}).get('result') != 'ok')
        return {**result, 'result': 'accepted' if uncertain else 'blocked',
                'reason': 'cargo_move_unconfirmed',
                'message': movement.get('message') or '本次前进或停稳结果尚未确认，需要核对原任务。'}
    stopped_at = time.monotonic()
    try:
        after = await _await_authorized(observe_cargo(cargo, destination,
            after_frame_id=before['frame_id'], after_received_at=stopped_at), context)
        _check_after_frame(before, after)
    except (TargetUnlocalized, ValueError) as exc:
        reason = getattr(exc, 'reason', str(exc))
        return {**result, 'result': 'blocked', 'reason': reason,
                'message': '本步机身动作已结束并停稳，但未能重新确认同一货物；尚未确认推动或送达。'}
    return _visual_result(cargo, destination, before, after, movement, turning, close)


def _delivery_plan(sample, pose):
    """Stand behind the cargo-to-destination axis, allowing a coarse angle."""
    cargo, target = sample['cargo_location'], sample.get('destination_location')
    if (not isinstance(target, dict) or any(type(target.get(k)) not in (int, float)
            or not math.isfinite(target[k]) for k in ('x', 'y'))):
        raise TargetUnlocalized('delivery_direction_unavailable')
    dx, dy = target['x'] - cargo['x'], target['y'] - cargo['y']
    if math.hypot(dx, dy) <= .01:
        raise TargetUnlocalized('delivery_direction_unavailable')
    heading = math.atan2(dy, dx)
    bearing = math.atan2(cargo['y'] - pose['y'], cargo['x'] - pose['x'])
    error = abs(math.atan2(math.sin(bearing-heading), math.cos(bearing-heading)))
    # 0.3 rad (~17 degrees) is coarse directional agreement, not exact docking.
    aligned = error <= .3
    box = sample['cargo_bbox_1000']
    from cloud.go2.perception.rgb_lidar import go2_camera_profile
    camera = go2_camera_profile(640, 360)
    distance = math.hypot(cargo['x']-pose['x'], cargo['y']-pose['y'])
    radius = distance * (box[2]-box[0]) / 1000 * 640 / camera['fx'] / 2
    keepout = ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M + radius
    standoff = keepout + .3  # leave room to stop and see the cargo before contact
    return {'heading': heading, 'aligned': aligned,
            'x': cargo['x']-standoff*math.cos(heading),
            'y': cargo['y']-standoff*math.sin(heading),
            'keepout': (cargo['x'], cargo['y'], keepout)}


async def _directional_delivery(cargo, destination, advance, before, context, ledger, identity):
    """One positioning or push step; contact may end inside the camera blind spot."""
    from cloud.go2.tools.executor import execute_tool

    snapshot, reason = navigator.snapshot()
    if reason:
        raise TargetUnlocalized(reason)
    pose, grid = snapshot
    if pose.get('session_id') != before['odom_session_id']:
        raise TargetUnlocalized('odom_session_mismatch')
    _ensure_stable(before['pose'], pose)
    plan = _delivery_plan(before, pose)
    target = before['cargo_location']
    bearing = math.atan2(target['y']-pose['y'], target['x']-pose['x']) - pose['heading']
    bearing = math.atan2(math.sin(bearing), math.cos(bearing))
    turning = abs(bearing) > .12
    close = (math.hypot(target['x']-pose['x'], target['y']-pose['y'])
             <= ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M + advance + .15
             or before['cargo_bbox_1000'][3] >= 950)
    progress = 'positioned' if not plan['aligned'] else 'aligned' if turning else 'push_attempted' if close else 'approached'
    movement = None
    navigation_receipts = []
    result = {'delivered': False, 'cargo_motion_confirmed': False,
              'cargo_before': before, 'cargo_after': None,
              'reference': _reference_for(before, cargo, destination),
              'push_heading_rad': plan['heading'], 'push_direction_precision': 'approximate_visual',
              'directional_push_attempted': False}
    try:
        with use_effect(ledger, identity):
            if not plan['aligned']:
                contact = (CargoContact(before, pose, None, context)
                           if before.get('contact_observation') is not None else None)
                navigation = await _await_authorized(navigator.go_to_coordinate(
                    plan['x'], plan['y'], owner_session_id=context.session_id,
                    heading=plan['heading'], route_keepout=plan['keepout'],
                    face_target=(target['x'], target['y']),
                    cargo_contact=contact,
                    speed_limit=.25, minimum_command_speed=.25, arrival_tolerance_m=.1), context,
                    on_result=navigation_receipts.append)
                stopped = await _stop_approach(context, bounded=True)
                movement = {'result': 'ok' if navigation.get('status') == 'succeeded' else 'blocked',
                            'reason': navigation.get('reason'), 'navigation': navigation,
                            'stop': stopped, 'message': navigation.get('message')}
                last_pose = navigation.get('last_trusted_pose') or {}
                if (last_pose.get('session_id') == pose.get('session_id')
                        and all(type(last_pose.get(key)) in (int, float)
                                and math.isfinite(last_pose[key]) for key in ('x', 'y'))):
                    travelled = _distance(pose, last_pose)
                    if travelled > .02 and navigation.get('status') != 'succeeded':
                        detail = ('后段未再观察到移动' if navigation.get('reason') == 'motion_not_observed'
                                  else navigation.get('message') or '站位未完成')
                        movement['message'] = (f'已换位约{travelled:.2f}米，但尚未完成本次站位；'
                                               f'{detail}，未执行向前推送。')
            elif turning:
                movement = await execute_tool('go2_move', {
                    'direction': 'turn_left' if bearing > 0 else 'turn_right',
                    'angle_deg': math.degrees(abs(bearing))}, context=context)
            else:
                contact = None
                if before.get('contact_observation') is not None:
                    contact = CargoContact(before, pose, advance, context)
                    grid = await _await_authorized(invoke_blocking(contact.grid_for, grid, pose), context)
                _check_forward_path(grid, pose, advance)
                with permit_contact(contact):
                    movement = await execute_tool('go2_move', {
                        'direction': 'forward', 'distance_m': advance, 'speed': .25}, context=context)
    except asyncio.CancelledError:
        await _stop_approach(context, bounded=True)
        raise
    except (TargetUnlocalized, ValueError) as exc:
        # A lease change after navigation must not erase the real movement.
        if movement is None and navigation_receipts:
            movement = {'navigation': navigation_receipts[-1], 'result': 'blocked'}
        stopped = await _stop_approach(context, bounded=True) if movement is not None else None
        return {**result, 'result': 'accepted' if stopped and stopped.get('result') != 'ok' else 'blocked',
                'reason': getattr(exc, 'reason', str(exc)), 'movement': movement, 'stop': stopped,
                'message': '本次位置调整或推送已中止；保留已有运动回执，未确认货物移动或送达。'}
    result.update(movement=movement, stop=movement.get('stop'))
    if movement.get('result') != 'ok' or (movement.get('stop') or {}).get('result') != 'ok':
        uncertain = (movement.get('result') in {'accepted', 'executing', 'timeout', 'unknown'}
                     or (movement.get('stop') or {}).get('result') != 'ok')
        return {**result, 'result': 'accepted' if uncertain else 'blocked',
                'reason': movement.get('reason') or 'cargo_move_unconfirmed',
                'message': movement.get('message') or '本次调整位置或前进未确认完成。'}
    attempted = progress == 'push_attempted'
    return {**result, 'result': 'ok', 'progress': progress, 'motion_performed': True,
            'directional_push_attempted': attempted,
            'confirmation': 'directional_move_and_stop' if attempted else 'positioning_and_stop',
            'message': ('已按货物到指定目的地的大致方向执行前进推送尝试并确认停稳；未复核货物位移或送达。' if attempted else
                        '已完成向货物背向指定目的地一侧的位置调整并停稳，下一步重新观察后对齐推送。' if progress == 'positioned' else
                        '已朝同一货物转身并停稳，下一步重新观察后继续。' if turning else
                        '已沿指定目的地方向接近同一货物并停稳，尚未确认接触。')}


def _visual_result(cargo, destination, before, after, movement, turning, close):
    result = {'delivered': False, 'cargo_before': before, 'movement': movement,
              'stop': movement.get('stop'), 'cargo_precision': 'approximate_visual',
              'nudge_attempted': close and not turning}
    # An approximate visual estimate cannot prove cargo displacement.
    # Keep the physical attempt useful without turning camera approach into a
    # successful push/delivery receipt.
    progress = 'aligned' if turning else 'push_attempted' if close else 'approached'
    return {**result, 'result': 'ok', 'progress': progress,
            'confirmation': 'visual_step_and_stop',
            'cargo_after': after, 'reference': _reference_for(after, cargo, destination),
            'destination_precision': 'approximate_visual_proximity',
            'message': ('已朝视觉确认的同一货物转身并停稳，可继续根据新观察接近。' if turning else
                        '已按视觉估计位置尝试推送并停稳；没有独立货物点云，尚未确认货物实际位移。' if close else
                        '已按视觉估计位置接近同一货物并停稳；尚未确认接触或推动。')}


async def push_cargo(cargo: str, destination: str | None = None, distance_m: float | None = None) -> dict:
    """One guarded step; directional delivery does not require post-contact vision."""
    from cloud.go2.tools.executor import execute_tool

    if (not isinstance(cargo, str) or not cargo.strip() or len(cargo) > 150
            or destination is not None and (not isinstance(destination, str)
                or not destination.strip() or len(destination) > 150)
            or distance_m is not None and (type(distance_m) not in (float, int)
                or not math.isfinite(distance_m) or distance_m <= 0)):
        return {'result': 'rejected', 'error': 'invalid_cargo_push_parameters', 'delivered': False}
    reference = _reference.get()
    if reference is not None:
        # References identify the original objects. Model wording may change
        # with viewpoint, but never replaces this run's confirmed order.
        cargo = reference.get('cargo') or cargo
        destination = reference.get('destination', destination)
    context = current_context()
    parent_effect = current_effect()
    request_id = _request.get()
    if request_id is None and parent_effect is not None:
        request_id = 'cargo_' + sha256('\0'.join(parent_effect[1]).encode()).hexdigest()[:32]
    request_id = request_id or 'cargo_' + uuid.uuid4().hex
    ledger = FileEffectLedger()
    identity = ('go2', 'cargo:' + context.session_id, request_id, 'bounded_move')
    parameters = {'cargo': cargo, 'destination': destination, 'distance_m': distance_m}
    reservation = ledger.reserve(*identity, 'go2_push_cargo', parameters)
    if reservation['reservation'] != 'new':
        return {**(reservation.get('result') or {
            'result': 'accepted', 'reason': 'prior_cargo_step_unconfirmed', 'delivered': False,
            'message': '原推送请求尚无可靠结果，需要按原请求继续核对。'}),
            'request_id': request_id, 'replayed': True}
    before = after = movement = None
    moving = False
    try:
        if _signal.get() is not None and destination is None:
            raise TargetUnlocalized('cargo_delivery_contract_changed')
        _authorize(context)
        observation = (observe_cargo(cargo, destination, reference=reference) if reference is not None
                       else observe_cargo(cargo, destination))
        before = await _await_authorized(observation, context)
        if reference is not None and reference.get('search_evidence') is not None:
            _check_search_reference(reference, before)
        elif reference is not None:
            if reference.get('odom_session_id') != before['odom_session_id']:
                raise TargetUnlocalized('odom_session_mismatch')
        if destination is not None and before['near_destination']:
            stopped = await _stop_approach(context, bounded=True)
            if stopped.get('result') != 'ok':
                raise TargetUnlocalized('stop_unconfirmed')
            result = {'result': 'ok', 'delivered': True,
                      'progress': 'already_near_destination', 'motion_performed': False,
                      'cargo_motion_confirmed': False, 'movement': None,
                      'confirmation': 'cargo_near_destination_and_stopped',
                      'destination_precision': 'approximate_visual_proximity',
                      'reference': _reference_for(before, cargo, destination),
                      'cargo_before': before, 'cargo_after': None, 'stop': stopped,
                      'message': '观察时货物原本就在指定目的地附近；本次推物检查仅原地观察并确认停稳，没有执行靠近或推送。'}
        elif destination is not None:
            result = await _directional_delivery(cargo, destination, _advance_distance(distance_m, before),
                                                 before, context, ledger, identity)
        elif before['cargo_location'].get('source') in {'visual_ground_estimate', 'visual_distance_estimate'}:
            result = await _visual_step(cargo, destination, _advance_distance(distance_m, before),
                                        before, context, ledger, identity)
        else:
            distance_m = _advance_distance(distance_m, before)
            forward = _aligned(before)
            # Approach and contact use the same target view; proximity only
            # describes progress and never decides whether cargo is an obstacle.
            uncertainty = before['cargo_location'].get('uncertainty_m', 0.)
            clearance = forward - uncertainty - ROBOT_HALF_LENGTH_M - SAFETY_MARGIN_M
            nudge_attempted = clearance <= distance_m + .06
            advance = distance_m
            snapshot, reason = navigator.snapshot()
            if reason:
                raise TargetUnlocalized(reason)
            pose, grid = snapshot
            if pose.get('session_id') != before['odom_session_id']:
                raise TargetUnlocalized('odom_session_mismatch')
            _ensure_stable(before['pose'], pose)
            contact = None
            if before.get('contact_observation') is not None:
                contact = CargoContact(before, pose, advance, context)
                grid = await _await_authorized(invoke_blocking(contact.grid_for, grid, pose), context)
            _check_forward_path(grid, pose, advance)
            moving = True
            # The child effect identity keeps native training/action accounting
            # tied to this original step, including cancellation uncertainty.
            with use_effect(ledger, identity), permit_contact(contact):
                movement = await execute_tool('go2_move',
                    {'direction': 'forward', 'distance_m': advance, 'speed': .25}, context=context)
            moving = False
            if movement.get('result') != 'ok' or (movement.get('stop') or {}).get('result') != 'ok':
                reason = movement.get('reason') or ''
                raise TargetUnlocalized(reason if reason.startswith('cargo_contact_') else 'cargo_move_unconfirmed')
            stopped_at = time.monotonic()
            after = await _await_authorized(observe_cargo(cargo, destination,
                after_frame_id=before['frame_id'], after_received_at=stopped_at), context)
            _check_after_frame(before, after)
            if after['cargo_location'].get('source') in {'visual_ground_estimate', 'visual_distance_estimate'}:
                result = _visual_result(cargo, destination, before, after, movement, False, nudge_attempted)
            else:
                displacement = _distance(before['cargo_location'], after['cargo_location'])
                if displacement > advance + .20:
                    raise TargetUnlocalized('cargo_identity_or_position_changed')
                dx = after['cargo_location']['x'] - before['cargo_location']['x']
                dy = after['cargo_location']['y'] - before['cargo_location']['y']
                along = dx * math.cos(before['pose']['heading']) + dy * math.sin(before['pose']['heading'])
                cross = -dx * math.sin(before['pose']['heading']) + dy * math.cos(before['pose']['heading'])
                # Absolute association uncertainty includes the cargo's own radius,
                # so it cannot be summed as independent centroid noise.
                # Compare the actual before/after cluster, requiring displacement
                # beyond two voxel cells. No extra camera calls estimate jitter.
                noise = max(.08, 2 * max(before['voxel_resolution_m'], after['voxel_resolution_m']))
                geometry_stable = _stable_cluster_shape(before, after)
                pushed = (nudge_attempted and geometry_stable and along > noise
                          and abs(cross) <= max(.08, along * .5))
                # A closer camera viewpoint alone cannot complete a delivery.
                # An already-near cargo is handled before movement above.
                delivered = (destination is not None and pushed
                             and after['near_destination'])
                approach_gain = (_distance(before['pose'], before['cargo_location'])
                                 - _distance(after['pose'], after['cargo_location']))
                if not delivered and not pushed and approach_gain <= .05:
                    raise TargetUnlocalized('cargo_motion_unconfirmed')
                result = {'result': 'ok', 'delivered': delivered,
                          'progress': 'delivered' if delivered else 'pushed' if pushed else 'approached',
                          'confirmation': ('cargo_near_destination_and_stopped' if delivered
                                           else 'measured_cargo_displacement_and_stop' if pushed else 'measured_approach_and_stop'),
                          'destination_precision': after.get('destination_precision'),
                          'cargo_displacement_m': round(displacement, 3),
                          'cargo_forward_displacement_m': round(along, 3),
                          'cargo_motion_noise_m': round(noise, 3),
                          'cargo_association_uncertainty_m': [before['cargo_location']['uncertainty_m'],
                                                              after['cargo_location']['uncertainty_m']],
                          'cargo_cluster_geometry_stable': geometry_stable,
                          'nudge_attempted': nudge_attempted,
                          'approach_gain_m': round(approach_gain, 3),
                          'reference': _reference_for(after, cargo, destination),
                          'cargo_before': before, 'cargo_after': after,
                          'movement': movement, 'stop': movement['stop'],
                          'message': ('货物已大致送到指定目的地附近，停稳后由一张新鲜画面复核。' if delivered else
                                      '测得货物沿本单方向移动，已停稳；尚未确认送达。' if pushed else
                                      '已按实测位移靠近同一货物并停稳，尚未确认货物移动或送达。')}
    except asyncio.CancelledError:
        await _stop_approach(context, bounded=True)
        raise
    except (TargetUnlocalized, ValueError) as exc:
        reason = getattr(exc, 'reason', str(exc))
        stopped = None
        if moving or movement is not None:
            stopped = await _stop_approach(context, bounded=True)
        uncertain = (moving and movement is None
                     or movement is not None and movement.get('result') in {'accepted', 'executing', 'timeout', 'unknown'}
                     or stopped is not None and stopped.get('result') != 'ok')
        result = {'result': 'accepted' if uncertain else 'blocked', 'reason': reason,
                  'delivered': False, 'cargo_before': before, 'cargo_after': after,
                  'movement': movement, 'stop': stopped,
                  'reference': _reference_for(after or before, cargo, destination) if after or before else reference,
                  'message': _REASONS.get(reason, '当前观测或设备条件不足，本次推送尚未确认完成。')}
        if (reason == 'cargo_move_unconfirmed' and not uncertain
                and movement is not None and (movement.get('stop') or {}).get('result') == 'ok'
                and movement.get('message')):
            result['message'] = movement['message'] + '尚未确认货物发生位移。'
    # Raw component points are an in-process allowance, not a public tool
    # parameter, checkpoint capability, or retained observation projection.
    for key in ('cargo_before', 'cargo_after'):
        if isinstance(result.get(key), dict):
            result[key] = {k: v for k, v in result[key].items() if k != 'contact_observation'}
    if destination is not None and result.get('result') == 'ok':
        # Positioning is a successful step, not completion of the user's push.
        # The next call refreshes the confirmed target before another motion.
        completed = ((result.get('directional_push_attempted') is True
                      and result.get('confirmation') == 'directional_move_and_stop'
                      or result.get('delivered') is True
                      and result.get('confirmation') == 'cargo_near_destination_and_stopped')
                     and (result.get('stop') or {}).get('result') == 'ok')
        if not completed:
            result['final'] = False
    result = {**result, 'request_id': request_id}
    ledger.mark_result(*identity, result)
    return result
