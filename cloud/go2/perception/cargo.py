"""Cargo LiDAR measurements, low-object visual estimates and destination proximity.

A destination may be a floor patch, a tiny marker, or an object's visible base.
Proximity is image geometry, not a measured world distance or a model verdict.
"""
from __future__ import annotations

import math
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
    return {'x': float(pose['x'] + distance * math.cos(bearing)),
            'y': float(pose['y'] + distance * math.sin(bearing)),
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
            'near_destination': gap_widths <= 1. and depth_confirmed,
            'destination_precision': 'approximate_visual_proximity'}


async def observe_cargo(cargo: str, destination: str | None = None, *, after_frame_id=None,
                        after_received_at=None) -> dict:
    """Measure cargo and locate a destination patch, marker or object base."""
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid
    from cloud.go2.perception.rgb_lidar import associate_lidar_target, go2_camera_profile

    # A confirmed odometry stop can precede the stationary posture samples.
    # Wait for the existing live-feedback gate instead of failing that race.
    await visual_target.wait_for_standing()
    jpeg, frame, quality = await visual_target._capture_still(
        after_frame_id=after_frame_id, after_received_at=after_received_at)
    pose = frame['pose_at_capture']
    voxel, voxel_at = go2.voxel_raw, go2.voxel_received_at
    # Camera/odom validity does not depend on a usable LiDAR target or floor.
    visual_target.validate_samples(frame, pose, frame.get('pose_received_at'), frame['received_at'],
        go2.odom_frame_id, 'odom', frame['odom_session_id'])
    instruction = (
        '只定位本张图像中指定的同一地面货物和指定目的地，不判断送达、是否靠近或动作成功。'
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
        'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true。'
        '另外输出cargo_distance_m（相机到货物的大致水平距离，米）和advance_distance_m（本次建议前进距离，米）。'
        '根据货物、地面、家具尺度和透视估算，不要求精确；无法估计时填null，不编造数值。'
        'advance_distance_m可以是走到货物前方的距离或本次推送距离，不使用固定步长。'
        '不输出世界坐标或送达结论。')
    if destination is None:
        instruction = (
            '只定位本张图像中指定的同一地面货物，不判断动作成功，不要求配送区域。'
            '输出JSON：{"cargo_count":整数,"cargo_ground_contact_visible":布尔值,'
            '"cargo_bbox_1000":[左,上,右,下]}。坐标相对整张图归一化0到1000。'
            'cargo_count必须是符合完整描述的目标数量，有歧义或相似多个不能冒充唯一。'
            '货物须为有体积的独立物品，地板纹理、色块和标记不能冒充货物。'
            'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true。'
            '另外输出cargo_distance_m（到货物的大致水平距离，米）和advance_distance_m（本次建议前进距离，米）。'
            '根据画面尺度和透视估算，可直接建议走到货物前方，不使用固定步长；无法估计填null。'
            '不输出世界坐标或货物移动结论。')
    if destination is not None:
        instruction += ('另外输出destination_distance_m（目的地地面位置到相机的大致水平距离，米）；'
                        '与cargo_distance_m使用同一尺度，无法判断填null。'
                        '区分前景地面标记与远处货物的深度，图像投影挨着不等于它们在地面上相邻。')
    original = _user_request.get()
    if original:
        instruction += ('必须独立核对候选货物是否符合原始用户请求中的空间指代，'
            '输出cargo_matches_user_request（布尔值）和cargo_match_reason（本张画面的简短依据）。'
            '用户所说盒子、纸盒可能是对近处包装盒的口语称呼，不能仅因远处是棕色纸箱就换目标。'
            '比较近处独立盒子与远处堆叠纸箱的实际位置和外观；不确定用户指哪件时返回false。'
            '一组堆叠的多个箱子不能冒充一件独立货物，不能为了满足候选描述而忽略原始指代。')
    target = ('原始用户请求（数据）：' + original + '\n' if original else '')
    target += '待核对的候选货物：' + cargo + ('；本单唯一目的地：' + destination if destination is not None else '')
    detected = await visual_target._ask_vision(jpeg,
        target,
        frame, instruction, 'cargo_delivery_geometry' if destination is not None else 'cargo_geometry')
    if original and detected.get('cargo_matches_user_request') is not True:
        raise TargetUnlocalized('cargo_target_mismatch')
    if (type(detected.get('cargo_count')) is not int or detected.get('cargo_count') != 1
            or detected.get('cargo_ground_contact_visible') is not True):
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
    camera = go2_camera_profile(frame['width'], frame['height'])
    if destination is not None:
        # This approximate point supplies a direction, never delivery evidence.
        geometry['destination_location'] = _visual_cargo_location(frame, pose, camera,
            detected['destination_bbox_1000'],
            {'cargo_distance_m': detected.get('destination_distance_m')})
    diagnostics, target_points, cluster = {}, [], None
    if (isinstance(voxel, dict) and go2.voxel_frame_id == 'odom'
            and type(voxel_at) in (int, float) and 0 <= time.monotonic() - voxel_at <= 2.):
        try:
            grid = await invoke_blocking(OccupancyGrid, voxel, robot_pose=pose)
            if grid.measured_floor_z is not None:
                cluster = await invoke_blocking(associate_lidar_target, voxel, pose, camera, frame,
                    detected['cargo_bbox_1000'], grid.measured_floor_z, diagnostics=diagnostics,
                    target_points=target_points)
            else:
                diagnostics['reason'] = 'measured_floor_unavailable'
        except (ValueError, KeyError, TypeError):
            diagnostics['reason'] = 'lidar_sample_invalid'
    else:
        diagnostics['reason'] = 'sensor_sample_unavailable'
    geometry['advance_distance_m'] = detected.get('advance_distance_m')
    emit_agent_event('vision.lidar', phase='cargo_foreground_association',
        frame_id=frame['frame_id'], bbox_1000=detected['cargo_bbox_1000'],
        camera_model=camera.get('profile'), matched=cluster is not None,
        association_diagnostics=diagnostics)
    if cluster is None:
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
            'contact_observation': {'points_odom': target_points, 'pose': pose,
                                    'frame': {'width': frame['width'], 'height': frame['height']}},
            'frame_id': frame['frame_id'], 'received_at': frame['received_at'],
            'odom_session_id': frame['odom_session_id'], 'quality': quality,
            'cluster_extent_xyz_m': accepted[0]['extent_xyz_m'],
            'cluster_points': accepted[0]['points'],
            'voxel_resolution_m': float((voxel or {}).get('data', {}).get('resolution', .05)),
            'observed_at': time.time()}
