"""Cargo LiDAR measurements, low-object visual estimates and destination proximity.

A destination may be a floor patch, a tiny marker, or an object's visible base.
Proximity is image geometry, not a measured world distance or a model verdict.
"""
from __future__ import annotations

import math
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar

from cloud.agents.common.runner import invoke_blocking
from cloud.go2.connection import go2
from cloud.go2.perception import visual_target
from cloud.go2.perception.visual_target import TargetUnlocalized
from cloud.observability import emit_agent_event

_user_request = ContextVar('cargo_original_user_request', default='')


@contextmanager
def cargo_request(user_request):
    """Keep the caller's original words separate from a model-selected target."""
    token = _user_request.set(user_request)
    try:
        yield
    finally:
        _user_request.reset(token)


def _visual_cargo_location(frame, pose, camera, box, detected):
    """Keep model-estimated distance explicit; never label it LiDAR evidence."""
    from cloud.go2.perception.rgb_lidar import image_bearing

    distance = detected.get('cargo_distance_m')
    if type(distance) not in (int, float) or not math.isfinite(distance) or distance <= 0:
        raise TargetUnlocalized('visual_distance_unavailable')
    bearing = pose['heading'] + image_bearing(camera, box, frame['width'], frame['height'])
    offset = camera['translation_body_from_camera_m']
    c, s = math.cos(pose['heading']), math.sin(pose['heading'])
    return {'x': float(pose['x'] + c * offset[0] - s * offset[1] + distance * math.cos(bearing)),
            'y': float(pose['y'] + s * offset[0] + c * offset[1] + distance * math.sin(bearing)),
            'estimated_distance_m': float(distance),
            'source': 'visual_distance_estimate', 'precision': 'approximate'}


def _destination_geometry(frame, detected):
    """Accept about one cargo width of separation at the visible floor level."""
    cargo, target = detected.get('cargo_bbox_1000'), detected.get('destination_bbox_1000')
    for box in (cargo, target):
        visual_target.target_offset({'found': True, 'bbox_1000': box}, frame)
    kind = detected.get('destination_kind')
    if kind not in {'region', 'marker', 'object'}:
        raise TargetUnlocalized('destination_geometry_invalid')
    # For an upright object, the visible base matters; its upper silhouette
    # cannot make a distant cargo seem close. Regions use the visible patch,
    # with no minimum area, full-outline or full-containment requirement.
    cx, cy = (cargo[0] + cargo[2]) / 2, cargo[3]
    if kind == 'object':
        tx, ty = (target[0] + target[2]) / 2, target[3]
    else:
        tx = min(max(cx, target[0]), target[2])
        ty = min(max(cy, target[1]), target[3])
    gap_px = math.hypot((cx - tx) * frame['width'] / 1000,
                        (cy - ty) * frame['height'] / 1000)
    cargo_width_px = (cargo[2] - cargo[0]) * frame['width'] / 1000
    gap_widths = gap_px / cargo_width_px
    # Pixel adjacency can put a distant box next to a foreground floor marker.
    # Cross-check coarse depth, without imposing a movement-distance limit.
    ranges = detected.get('cargo_distance_m'), detected.get('destination_distance_m')
    depth_confirmed = all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in ranges)
    if depth_confirmed:
        from cloud.go2.perception.rgb_lidar import go2_camera_profile
        width_m = ranges[0] * cargo_width_px / go2_camera_profile(frame['width'], frame['height'])['fx']
        depth_confirmed = abs(ranges[0] - ranges[1]) <= width_m
    return {'cargo_bbox_1000': cargo, 'destination_bbox_1000': target,
            'destination_kind': kind, 'destination_anchor_1000': [tx, ty],
            'destination_gap_cargo_widths': round(gap_widths, 3),
            'destination_depth_confirmed': depth_confirmed,
            'near_destination': (gap_widths <= 1. and depth_confirmed
                                 and detected.get('cargo_ground_contact_visible') is not False),
            'destination_precision': 'approximate_visual_proximity'}


def _cargo_map(session):
    """Use the completed shared map; cargo perception never rebuilds it."""
    grid = getattr(go2, 'occupancy_grid', None)
    stamp = getattr(grid, 'observed_at', None)
    if (grid is None or getattr(grid, 'session_id', None) != session
            or getattr(grid, 'frame_id', None) != 'odom'
            or type(stamp) not in (int, float) or not 0 <= time.monotonic()-stamp <= 2.):
        return None
    return grid


def _lidar_range_agrees(cluster, pose, detected, frame, camera, diagnostics):
    """A box overlap alone cannot associate background returns to the cargo.

    Compare the cluster's measured uncertainty and the target's apparent size
    with the independent visual range. Disagreement selects the existing visual
    estimate; it never becomes permission to chase the farther background.
    """
    estimate = detected.get('cargo_distance_m')
    if type(estimate) not in (int, float) or not math.isfinite(estimate) or estimate <= 0:
        return True  # Valid LiDAR can still supply range without a visual estimate.
    measured = math.hypot(cluster['x']-pose['x'], cluster['y']-pose['y'])
    box = detected['cargo_bbox_1000']
    apparent_size = estimate * max(
        (box[2]-box[0]) * frame['width'] / 1000 / camera['fx'],
        (box[3]-box[1]) * frame['height'] / 1000 / camera['fy'])
    allowance = cluster.get('uncertainty_m', 0.) + apparent_size
    agrees = abs(measured-estimate) <= allowance
    diagnostics.update(visual_range_m=estimate, lidar_range_m=measured,
                       range_agreement_m=allowance, range_agrees=agrees)
    if not agrees:
        diagnostics['reason'] = 'lidar_visual_range_disagreement'
    return agrees


async def _track_confirmed_cargo(reference):
    """Refresh a known object's range from LiDAR; old image facts keep their age."""
    from cloud.go2.perception.rgb_lidar import associate_lidar_target, go2_camera_profile

    previous = reference.get('observation') or {}
    frame = previous.get('image_size')
    capture_pose = previous.get('capture_pose')
    if not frame or not capture_pose or previous.get('cargo_location', {}).get('source') != 'lidar_foreground_cluster':
        return None
    session = reference.get('odom_session_id')
    pose = visual_target._live_pose(session)
    grid = _cargo_map(session)
    if grid is None or grid.measured_floor_z is None:
        return None
    voxel, stamp = grid.voxel_message, grid.observed_at
    diagnostics = {}
    target = await invoke_blocking(associate_lidar_target, voxel, capture_pose,
        go2_camera_profile(frame['width'], frame['height']), frame,
        previous['cargo_bbox_1000'], grid.measured_floor_z,
        diagnostics=diagnostics)
    prior = reference.get('cargo_location') or {}
    if target is not None and all(type(item.get('uncertainty_m')) in (int, float)
                                  for item in (target, prior)):
        if math.hypot(target['x']-prior['x'], target['y']-prior['y']) > (
                target['uncertainty_m'] + prior['uncertainty_m']):
            target = None
            diagnostics['reason'] = 'reference_region_disagreement'
    emit_agent_event('vision.lidar', phase='cargo_reference_tracking', matched=target is not None,
                     association_diagnostics=diagnostics)
    if target is None:
        return None
    live = visual_target._live_pose(session)
    visual_target._ensure_stable(pose, live)
    advance = previous.get('advance_distance_m')
    destination = previous.get('destination_location')
    if reference.get('destination') is not None and destination:
        from cloud.go2.navigation.occupancy_grid import ROBOT_HALF_LENGTH_M
        # A previous visual approach distance belongs to the previous pose.
        # Recompute from the updated cargo and the same destination instead
        # of repeating that old distance after the dog has already approached.
        advance = (max(0., math.hypot(target['x']-live['x'], target['y']-live['y']) - ROBOT_HALF_LENGTH_M)
                   + math.hypot(destination['x']-target['x'], destination['y']-target['y']))
        if advance <= 0:
            return None
    accepted = next(component for component in diagnostics['components'] if not component.get('rejected_by'))
    return {**previous, 'pose': live, 'near_destination': False,
            'cargo_location': {**target, 'source': 'lidar_foreground_cluster',
                               'frame_id': 'odom', 'odom_session_id': session},
            'advance_distance_m': advance,
            'cluster_extent_xyz_m': accepted['extent_xyz_m'], 'cluster_points': accepted['points'],
            'tracking_source': 'lidar', 'lidar_received_at': stamp, 'observed_at': time.time()}


async def observe_cargo(cargo: str, destination: str | None = None, *, after_frame_id=None,
                        after_received_at=None, reference: dict | None = None) -> dict:
    """Measure cargo and locate a destination patch, marker or object base."""
    from cloud.go2.perception.rgb_lidar import associate_lidar_target, go2_camera_profile

    source = ((reference or {}).get('cargo_location') or {}).get('source')
    visual_source = source in {'visual_ground_estimate', 'visual_distance_estimate'}
    # A confirmed odometry stop can precede the stationary posture samples.
    # Wait for the existing live-feedback gate instead of failing that race.
    await visual_target.wait_for_standing()
    if reference is not None and not visual_source and after_frame_id is None and after_received_at is None:
        tracked = await _track_confirmed_cargo(reference)
        if tracked is not None:
            return tracked
    jpeg, frame, quality = await visual_target._capture_still(
        after_frame_id=after_frame_id, after_received_at=after_received_at)
    pose = frame['pose_at_capture']
    # Camera/odom validity does not depend on a usable LiDAR target or floor.
    visual_target.validate_samples(frame, pose, frame.get('pose_received_at'), frame['received_at'],
        go2.odom_frame_id, 'odom', frame['odom_session_id'])
    instruction = (
        '报告本张图像中指定的同一地面货物和指定目的地的可见位置与粗略相对方位，不判断送达或动作成功。'
        '输出JSON：{"cargo_count":整数,"destination_count":整数,"cargo_ground_contact_visible":布尔值,'
        '"destination_ground_contact_visible":布尔值,"destination_kind":"region或marker或object",'
        '"cargo_bbox_1000":[左,上,右,下],"destination_bbox_1000":[左,上,右,下]}。'
        '坐标相对整张图归一化0到1000。目的地可为一片色块、小标记或用户指定的具体物体；'
        'region为地面区域，marker为地面小标记，object为物体标识，bbox下边缘须对应其可见底部。'
        '目的地不需要完整边界、特定面积或能够容纳货物；只框出当前实际可见部分，不补画遮挡边界。'
        'cargo_count和destination_count必须分别是符合完整描述的目标数量，有歧义或相似多个不能选一个冒充唯一。'
        'destination_ground_contact_visible表示能看到指定区域在地面的位置或物体底部与地面的接触处；'
        '同色但不符合指定描述的杂物不能冒充目的地，货物本身不能冒充目的地。'
        '货物须为有体积的独立物品，地板纹理、色块和标记不能冒充货物。'
        'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true；'
        '货物靠近画面下边缘或底部被裁切仍可辨认，不因此将cargo_count置零，也不要求后退。'
        '另外输出cargo_distance_m（相机到货物的大致水平距离，米）和advance_distance_m（本次建议前进距离，米）。'
        '根据货物、地面、家具尺度和透视估算，不要求精确；无法估计时填null，不编造数值。'
        'advance_distance_m可以是走到货物前方的距离或本次推送距离，不使用固定步长。'
        '另外输出cargo_nearby（布尔值）：货物已在机身前方近处、可以开始调整站位推送时为true；'
        '仍需先走过一段地面接近时为false。advance_distance_m是建议机身实际前进的距离，'
        '由执行器直接执行，不会再扣机身尺寸、货物半径或观察留距。'
        '另外输出push_view（简短中文）：描述货物是否位于身前可接触区域、目的地相对货物的大致方向、'
        '当前朝前推是否大致朝目的地，以及观察中的不确定性，供机器狗原Graph判断。'
        '分别描述机身到货物、货物到目的地的方位；只报告观察，不建议左转、右转或换位动作。'
        '目的地在画面左侧不等于机身应左转，不能把目的地方位当成接触货物的朝向。'
        '不要求货物中心精确在画面中线；近处裁切框的中心不等于货物真实中心，'
        '不得据此断言必须横移或精确共线，不指定世界坐标站位。'
        '不输出世界坐标或送达结论。')
    if destination is None:
        instruction = (
            '只定位本张图像中指定的同一地面货物，不判断动作成功，不要求配送区域。'
            '输出JSON：{"cargo_count":整数,"cargo_ground_contact_visible":布尔值,'
            '"cargo_bbox_1000":[左,上,右,下]}。坐标相对整张图归一化0到1000。'
            'cargo_count必须是符合完整描述的目标数量，有歧义或相似多个不能冒充唯一。'
            '货物须为有体积的独立物品，地板纹理、色块和标记不能冒充货物。'
            'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true；'
            '货物靠近画面下边缘或底部被裁切仍可辨认，不因此将cargo_count置零，也不要求后退。'
            '另外输出cargo_distance_m（到货物的大致水平距离，米）和advance_distance_m（本次建议前进距离，米）。'
            '根据画面尺度和透视估算，可直接建议走到货物前方，不使用固定步长；无法估计填null。'
            '不输出世界坐标或货物移动结论。')
    if destination is not None:
        instruction += ('另外输出destination_distance_m（目的地地面位置到相机的大致水平距离，米）；'
                        '与cargo_distance_m使用同一尺度，无法判断填null。'
                        '区分前景地面标记与远处货物的深度，图像投影挨着不等于它们在地面上相邻。')
    original = _user_request.get() if reference is None else ''
    if original:
        instruction += ('必须独立核对候选货物是否符合原始用户请求中的空间指代，'
            '输出cargo_matches_user_request（布尔值）和cargo_match_reason（本张画面的简短依据）。'
            '用户所说盒子、纸盒可能是对近处包装盒的口语称呼，不能仅因远处是棕色纸箱就换目标。'
            '比较近处独立盒子与远处堆叠纸箱的实际位置和外观；不确定用户指哪件时返回false。'
            '一组堆叠的多个箱子不能冒充一件独立货物，不能为了满足候选描述而忽略原始指代。')
    target = ('原始用户请求（数据）：' + original + '\n' if original else '')
    target += '待核对的候选货物：' + cargo + ('；本单唯一目的地：' + destination if destination is not None else '')
    if reference is not None:
        instruction += ('本单已有前次真实观察引用。原话和候选中的前面、左侧、右侧、脚边等空间指代'
            '属于首次确认时的视角，换位后不能要求货物仍处于同一画面方位。'
            '结合原物体外观、前次位置与当前机身位置重新核对同一货物；'
            '前次估计位置不证明当前可见，不得补画、换成背景同类物体或把估计当精确测量。')
        target += '\n前次工具观察引用与当前机身位置（数据）：' + json.dumps({
            'cargo': reference.get('cargo'), 'destination': reference.get('destination'),
            'cargo_location': reference.get('cargo_location'),
            'odom_session_id': reference.get('odom_session_id'), 'current_pose': pose}, ensure_ascii=False)
    detected = await visual_target._ask_vision(jpeg,
        target,
        frame, instruction, 'cargo_delivery_geometry' if destination is not None else 'cargo_geometry')
    if original and detected.get('cargo_matches_user_request') is not True:
        raise TargetUnlocalized('cargo_target_mismatch')
    if type(detected.get('cargo_count')) is not int or detected.get('cargo_count') != 1:
        raise TargetUnlocalized('cargo_ambiguous' if destination is None else 'cargo_or_destination_ambiguous')
    if destination is not None:
        if (type(detected.get('destination_count')) is not int or detected.get('destination_count') != 1
                or detected.get('destination_ground_contact_visible') is not True):
            raise TargetUnlocalized('cargo_or_destination_ambiguous')
        geometry = _destination_geometry(frame, detected)
    else:
        visual_target.target_offset({'found': True, 'bbox_1000': detected.get('cargo_bbox_1000')}, frame)
        geometry = {'cargo_bbox_1000': detected['cargo_bbox_1000'], 'near_destination': False,
                    'destination_anchor_1000': None}
    live = visual_target._live_pose(frame['odom_session_id'])
    visual_target._ensure_stable(pose, live)
    # Model latency must not age out a snapshot saved before inference. The
    # robot has remained stationary; associate a fresh scan with this image
    # only while its pose and odometry session still agree.
    if getattr(go2, '_odom_session_id', frame['odom_session_id']) != frame['odom_session_id']:
        raise TargetUnlocalized('odom_session_mismatch')
    # Choose LiDAR first on the initial observation. Once this order falls
    # back to visual range, later clusters must never replace that source.
    grid = None if visual_source else _cargo_map(frame['odom_session_id'])
    voxel = grid.voxel_message if grid is not None else None
    camera = go2_camera_profile(frame['width'], frame['height'])
    if destination is not None:
        # This approximate point supplies a direction, never delivery evidence.
        geometry['destination_location'] = _visual_cargo_location(frame, pose, camera,
            detected['destination_bbox_1000'],
            {'cargo_distance_m': detected.get('destination_distance_m')})
        estimate = detected.get('cargo_distance_m')
        if type(estimate) in (int, float) and math.isfinite(estimate) and estimate > 0:
            geometry['cargo_direction_location'] = _visual_cargo_location(
                frame, pose, camera, detected['cargo_bbox_1000'], detected)
    diagnostics, cluster = {}, None
    if grid is not None:
        try:
            if grid.measured_floor_z is not None:
                cluster = await invoke_blocking(associate_lidar_target, voxel, pose, camera, frame,
                    detected['cargo_bbox_1000'], grid.measured_floor_z, diagnostics=diagnostics)
            else:
                diagnostics['reason'] = 'measured_floor_unavailable'
        except (ValueError, KeyError, TypeError):
            diagnostics['reason'] = 'lidar_sample_invalid'
    else:
        diagnostics['reason'] = 'visual_source_locked' if visual_source else 'sensor_sample_unavailable'
    if cluster is not None and not _lidar_range_agrees(cluster, pose, detected, frame, camera, diagnostics):
        cluster = None
    geometry['advance_distance_m'] = detected.get('advance_distance_m')
    geometry['cargo_nearby'] = detected.get('cargo_nearby') is True
    view = detected.get('push_view')
    geometry['push_view'] = view[:1600] if isinstance(view, str) else ''
    geometry['capture_pose'] = pose
    geometry['image_size'] = {key: frame[key] for key in ('width', 'height')}
    emit_agent_event('vision.lidar', phase='cargo_foreground_association',
        frame_id=frame['frame_id'], bbox_1000=detected['cargo_bbox_1000'],
        camera_model=camera.get('profile'), matched=cluster is not None,
        localization_source=source,
        association_diagnostics=diagnostics)
    if cluster is None:
        if source == 'lidar_foreground_cluster':
            raise TargetUnlocalized('cargo_returns_unavailable')
        location = _visual_cargo_location(frame, pose, camera,
            detected['cargo_bbox_1000'], detected)
        live = visual_target._live_pose(frame['odom_session_id'])
        visual_target._ensure_stable(pose, live)
        emit_agent_event('vision.lidar', phase='cargo_visual_estimate', frame_id=frame['frame_id'],
            source=location['source'], precision=location['precision'], matched=False)
        return {**geometry, 'cargo_location': {
                    **location, 'frame_id': 'odom', 'odom_session_id': frame['odom_session_id'],
                    'source_frame_id': frame['frame_id'], 'source_received_at': frame['received_at']},
                'pose': live, 'frame_id': frame['frame_id'], 'received_at': frame['received_at'],
                'odom_session_id': frame['odom_session_id'], 'quality': quality,
                'observed_at': time.time()}
    live = visual_target._live_pose(frame['odom_session_id'])
    visual_target._ensure_stable(pose, live)
    location = {**cluster, 'frame_id': 'odom', 'odom_session_id': frame['odom_session_id'],
                'source_frame_id': frame['frame_id'], 'source_received_at': frame['received_at'],
                'source': 'lidar_foreground_cluster'}
    accepted = [item for item in diagnostics.get('components', []) if not item.get('rejected_by')]
    if len(accepted) != 1:
        raise TargetUnlocalized('cargo_association_unconfirmed')
    return {**geometry, 'cargo_location': location, 'pose': live,
            'frame_id': frame['frame_id'], 'received_at': frame['received_at'],
            'odom_session_id': frame['odom_session_id'], 'quality': quality,
            'cluster_extent_xyz_m': accepted[0]['extent_xyz_m'],
            'cluster_points': accepted[0]['points'],
            'voxel_resolution_m': float((voxel or {}).get('data', {}).get('resolution', .05)),
            'observed_at': time.time()}
