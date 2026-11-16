"""Cargo LiDAR measurements and approximate visual destination proximity.

A destination may be a floor patch, a tiny marker, or an object's visible base.
Proximity is image geometry, not a measured world distance or a model verdict.
"""
from __future__ import annotations

import math
import time

from cloud.agents.common.runner import invoke_blocking
from cloud.go2.connection import go2
from cloud.go2.perception import visual_target
from cloud.go2.perception.visual_target import TargetUnlocalized


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
    return {'cargo_bbox_1000': cargo, 'destination_bbox_1000': target,
            'destination_kind': kind, 'destination_anchor_1000': [tx, ty],
            'destination_gap_cargo_widths': round(gap_widths, 3),
            'near_destination': gap_widths <= 1.,
            'destination_precision': 'approximate_visual_proximity'}


async def observe_cargo(cargo: str, destination: str | None = None, *, after_frame_id=None,
                        after_received_at=None) -> dict:
    """Measure cargo and locate a destination patch, marker or object base."""
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid
    from cloud.go2.perception.rgb_lidar import associate_lidar_target, go2_camera_profile

    state_at = getattr(go2, '_sport_state_observed_at', None)
    if (getattr(go2, 'body_state', None) != 'standing' or state_at is None
            or not 0 <= time.monotonic() - state_at <= 1.):
        raise TargetUnlocalized('posture_not_confirmed')
    jpeg, frame, quality = await visual_target._capture_still(
        after_frame_id=after_frame_id, after_received_at=after_received_at)
    pose = frame['pose_at_capture']
    voxel, voxel_at = go2.voxel_raw, go2.voxel_received_at
    visual_target.validate_samples(frame, pose, frame.get('pose_received_at'), voxel_at,
        go2.odom_frame_id, go2.voxel_frame_id, frame['odom_session_id'])
    grid = await invoke_blocking(OccupancyGrid, voxel, robot_pose=pose)
    if grid.measured_floor_z is None:
        raise TargetUnlocalized('measured_floor_unavailable')
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
        'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true。'
        '不输出世界坐标、距离或送达结论。')
    if destination is None:
        instruction = (
            '只定位本张图像中指定的同一地面货物，不判断动作成功，不要求配送区域。'
            '输出JSON：{"cargo_count":整数,"cargo_ground_contact_visible":布尔值,'
            '"cargo_bbox_1000":[左,上,右,下]}。坐标相对整张图归一化0到1000。'
            'cargo_count必须是符合完整描述的目标数量，有歧义或相似多个不能冒充唯一。'
            'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true。'
            '不输出世界坐标、距离或货物移动结论。')
    detected = await visual_target._ask_vision(jpeg,
        '货物：' + cargo + ('；本单唯一目的地：' + destination if destination is not None else ''),
        frame, instruction, 'cargo_delivery_geometry' if destination is not None else 'cargo_geometry')
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
    diagnostics = {}
    cluster = await invoke_blocking(associate_lidar_target, voxel, pose, camera, frame,
        detected['cargo_bbox_1000'], grid.measured_floor_z, diagnostics=diagnostics)
    if cluster is None:
        raise TargetUnlocalized('cargo_returns_unavailable')
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
            'voxel_resolution_m': float(voxel['data'].get('resolution', .05)),
            'observed_at': time.time()}
