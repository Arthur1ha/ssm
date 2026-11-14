"""One measured, bounded cargo nudge using ordinary guarded Go2 gait.

This adapter never removes cargo from the obstacle map or invents a foot RPC.
If the existing body sweep forbids contact, pushing is blocked. Progress and
delivery are determined from cargo observations, never robot arrival alone.
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
from cloud.go2.control import control_lease
from cloud.go2.navigation.navigator import navigator
from cloud.go2.navigation.occupancy_grid import ROBOT_HALF_LENGTH_M, SAFETY_MARGIN_M
from cloud.go2.navigation.target_approach import _authorize, _await_authorized, _stop_approach
from cloud.go2.perception.cargo import observe_cargo
from cloud.go2.perception.visual_target import TargetUnlocalized, _ensure_stable
from cloud.space.execution import current_context

_reference: ContextVar[dict | None] = ContextVar('cargo_push_reference', default=None)
_request: ContextVar[str | None] = ContextVar('cargo_push_request', default=None)
_signal: ContextVar[tuple | None] = ContextVar('cargo_push_signal', default=None)

_REASONS = {
    'cargo_destination_alignment_required': '货物与指定配送区域尚未在同一推送方向，需要先重新对齐。',
    'cargo_or_destination_ambiguous': '当前画面无法唯一确认货物和完整的指定地面配送区域。',
    'delivery_polygon_invalid': '指定配送区域的可见边界不足以可靠核验。',
    'delivery_boundary_truncated': '指定配送区域边界未完整进入画面，暂不确认送达。',
    'delivery_color_unverified': '尚未从实际画面确认配送区域的颜色。',
    'delivery_color_not_uniform': '配送区域的颜色或范围尚不明确，需要重新观察。',
    'delivery_zone_signal_mismatch': '画面中的配送区域颜色与灯已确认的指令颜色不一致。',
    'delivery_zone_changed': '当前配送区域与本单先前确认的区域颜色不一致。',
    'delivery_region_not_connected': '当前画面不能确认一个完整连续的配送区域。',
    'cargo_returns_unavailable': '雷达尚不能可靠分离这件货物，未开始推送。',
    'cargo_association_unconfirmed': '货物的视觉与雷达对应关系尚未确认。',
    'cargo_contact_geometry_unavailable': '当前测量不足以确认可靠的靠近或腿部接触位置。',
    'cargo_contact_path_blocked': '机身避障检查未通过，靠近或接触货物的通路受阻。',
    'cargo_motion_unconfirmed': '尚未测得可靠的货物移动，本次不算推送完成。',
    'cargo_move_unconfirmed': '本次前进或停稳结果尚未确认，需要核对原任务。',
    'cargo_not_stable_or_identity_changed': '两次观察中的货物位置不稳定，或无法确认是同一件货物。',
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


def _same_stationary_cargo(first, second):
    if (first['odom_session_id'] != second['odom_session_id']
            or first['frame_id'] == second['frame_id']
            or second['received_at'] <= first['received_at']):
        raise TargetUnlocalized('cargo_observation_session_changed')
    _ensure_stable(first['pose'], second['pose'])
    jitter = _distance(first['cargo_location'], second['cargo_location'])
    if jitter > .06:
        raise TargetUnlocalized('cargo_not_stable_or_identity_changed')
    return jitter


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
            'zone_color_hsv': sample['zone_color_hsv']}


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
    box = sample['cargo_bbox_1000']
    zone = sample['zone_center_1000']
    # Screen geometry only constrains the push direction. It supplies neither
    # world distances nor successful movement evidence.
    if (forward <= 0 or abs(lateral) > .10
            or abs(math.atan2(lateral, forward)) > .18
            or abs((box[0] + box[2]) / 2 - 500) > 100
            or abs(zone[0] - 500) > 150
            or zone[1] >= box[3] - 10):
        raise TargetUnlocalized('cargo_destination_alignment_required')
    return forward


async def push_cargo(cargo: str, destination: str, distance_m: float = .15) -> dict:
    """Observe twice, make at most one measured forward move, then reobserve."""
    from cloud.go2.tools.executor import execute_tool

    if (not isinstance(cargo, str) or not cargo.strip() or len(cargo) > 150
            or not isinstance(destination, str) or not destination.strip() or len(destination) > 150
            or type(distance_m) not in (float, int) or not math.isfinite(distance_m)
            or not .1 <= distance_m <= .25):
        return {'result': 'rejected', 'error': 'invalid_cargo_push_parameters', 'delivered': False}
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
    reference = _reference.get()
    before = after = movement = None
    moving = False
    try:
        if reference is not None and (reference.get('cargo') != cargo
                or reference.get('destination') != destination):
            raise TargetUnlocalized('cargo_delivery_contract_changed')
        _authorize(context)
        hsv = (reference or {}).get('zone_color_hsv')
        signal = _signal.get()
        first = await _await_authorized(observe_cargo(cargo, destination,
            expected_hsv=hsv, expected_rgb=signal), context)
        before = await _await_authorized(observe_cargo(cargo, destination,
            after_frame_id=first['frame_id'], after_received_at=first['received_at'],
            expected_hsv=first['zone_color_hsv'], expected_rgb=signal), context)
        before_jitter = _same_stationary_cargo(first, before)
        if reference is not None and reference.get('search_evidence') is not None:
            _check_search_reference(reference, before)
        elif reference is not None:
            if reference.get('odom_session_id') != before['odom_session_id']:
                raise TargetUnlocalized('odom_session_mismatch')
            if _distance(reference['cargo_location'], before['cargo_location']) > .20:
                raise TargetUnlocalized('cargo_identity_or_position_changed')
        if first['contained'] and before['contained']:
            stopped = await _stop_approach(context, bounded=True)
            if stopped.get('result') != 'ok':
                raise TargetUnlocalized('stop_unconfirmed')
            result = {'result': 'ok', 'delivered': True,
                      'confirmation': 'cargo_contained_in_verified_zone_and_stopped',
                      'reference': _reference_for(before, cargo, destination),
                      'cargo_after': before, 'stop': stopped,
                      'message': '同一货物已由两张新鲜停稳画面确认完整位于本单指定颜色区域内。'}
        else:
            forward = _aligned(before)
            # The target's conservative measured radius remains occupied. A
            # distant box gets one safe approach increment; near it we attempt
            # the same ordinary gait nudge only if the full body sweep permits.
            uncertainty = before['cargo_location'].get('uncertainty_m')
            if type(uncertainty) not in (int, float) or not math.isfinite(uncertainty):
                raise TargetUnlocalized('cargo_contact_geometry_unavailable')
            clearance = forward - uncertainty - ROBOT_HALF_LENGTH_M - SAFETY_MARGIN_M
            nudge_attempted = clearance <= distance_m + .06
            advance = min(distance_m, clearance - .06) if clearance > distance_m + .06 else distance_m
            if advance < .1:
                raise TargetUnlocalized('cargo_contact_geometry_unavailable')
            snapshot, reason = navigator.snapshot()
            if reason:
                raise TargetUnlocalized(reason)
            pose, grid = snapshot
            if pose.get('session_id') != before['odom_session_id']:
                raise TargetUnlocalized('odom_session_mismatch')
            _ensure_stable(before['pose'], pose)
            for offset in (advance * index / 5 for index in range(6)):
                if not grid.is_body_clear(pose['x'] + offset * math.cos(pose['heading']),
                                          pose['y'] + offset * math.sin(pose['heading']), pose['heading']):
                    raise TargetUnlocalized('cargo_contact_path_blocked')
            moving = True
            # The child effect identity keeps native training/action accounting
            # tied to this original step, including cancellation uncertainty.
            with use_effect(ledger, identity):
                movement = await execute_tool('go2_move',
                    {'direction': 'forward', 'distance_m': advance, 'speed': .25}, context=context)
            moving = False
            if movement.get('result') != 'ok' or (movement.get('stop') or {}).get('result') != 'ok':
                raise TargetUnlocalized('cargo_move_unconfirmed')
            stopped_at = time.monotonic()
            candidate = await _await_authorized(observe_cargo(cargo, destination,
                after_frame_id=before['frame_id'], after_received_at=stopped_at,
                expected_hsv=before['zone_color_hsv'], expected_rgb=signal), context)
            after = await _await_authorized(observe_cargo(cargo, destination,
                after_frame_id=candidate['frame_id'], after_received_at=candidate['received_at'],
                expected_hsv=before['zone_color_hsv'], expected_rgb=signal), context)
            after_jitter = _same_stationary_cargo(candidate, after)
            if before['odom_session_id'] != after['odom_session_id']:
                raise TargetUnlocalized('odom_session_mismatch')
            displacement = _distance(before['cargo_location'], after['cargo_location'])
            if displacement > advance + .20:
                raise TargetUnlocalized('cargo_identity_or_position_changed')
            dx = after['cargo_location']['x'] - before['cargo_location']['x']
            dy = after['cargo_location']['y'] - before['cargo_location']['y']
            along = dx * math.cos(before['pose']['heading']) + dy * math.sin(before['pose']['heading'])
            cross = -dx * math.sin(before['pose']['heading']) + dy * math.cos(before['pose']['heading'])
            # Absolute association uncertainty includes the cargo's own radius,
            # so it cannot be summed as independent centroid noise. Instead
            # require the same stable compact component before/after, a shift
            # exceeding two measured voxel cells AND three times observed
            # stationary jitter. Keep absolute uncertainty visible in receipts.
            noise = max(.08, 2 * max(before['voxel_resolution_m'], after['voxel_resolution_m']),
                        3 * (before_jitter + after_jitter))
            geometry_stable = all((_stable_cluster_shape(first, before),
                                   _stable_cluster_shape(before, candidate),
                                   _stable_cluster_shape(candidate, after)))
            pushed = (nudge_attempted and geometry_stable and along > noise
                      and abs(cross) <= max(.08, along * .5))
            delivered = candidate['contained'] and after['contained']
            approach_gain = (_distance(before['pose'], before['cargo_location'])
                             - _distance(after['pose'], after['cargo_location']))
            if not delivered and not pushed and approach_gain <= .05:
                raise TargetUnlocalized('cargo_motion_unconfirmed')
            result = {'result': 'ok', 'delivered': delivered,
                      'progress': 'delivered' if delivered else 'pushed' if pushed else 'approached',
                      'confirmation': ('cargo_contained_in_verified_zone_and_stopped' if delivered
                                       else 'measured_cargo_displacement_and_stop' if pushed else 'measured_approach_and_stop'),
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
                      'message': ('货物已在指定颜色区域内，停稳后由两张新鲜画面复核。' if delivered else
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
    result = {**result, 'request_id': request_id}
    ledger.mark_result(*identity, result)
    return result
