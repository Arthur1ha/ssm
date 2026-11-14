"""Paired cargo/colored-zone geometry from stationary camera and measured LiDAR.

Vision proposes image regions. Actual image-color connectivity verifies the
zone outline; LiDAR supplies cargo odom coordinates. No model delivery verdict
or image-size change is interpreted as cargo movement.
"""
from __future__ import annotations

import math
import time

import cv2
import numpy as np

from cloud.agents.common.runner import invoke_blocking
from cloud.go2.connection import go2
from cloud.go2.perception import visual_target
from cloud.go2.perception.visual_target import TargetUnlocalized


def _points(value, width, height):
    if (not isinstance(value, list) or not 3 <= len(value) <= 12
            or any(not isinstance(point, list) or len(point) != 2
                   or any(type(v) not in (int, float) or not math.isfinite(v)
                          or not 0 <= v <= 1000 for v in point) for point in value)):
        raise TargetUnlocalized('delivery_polygon_invalid')
    points = np.asarray(value, dtype=np.float32) * [width / 1000, height / 1000]
    contour = np.rint(points).astype(np.int32).reshape(-1, 1, 2)
    if not cv2.isContourConvex(contour) or cv2.contourArea(contour) < width * height * .005:
        raise TargetUnlocalized('delivery_polygon_invalid')
    return contour


def _zone_geometry(jpeg, frame, detected, expected_hsv=None, expected_rgb=None):
    """Check actual colored pixels and containment, independently of prose."""
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (frame['height'], frame['width']):
        raise TargetUnlocalized('frame_dimensions_invalid')
    height, width = image.shape[:2]
    box = detected.get('cargo_bbox_1000')
    # Reuse the existing bbox validator rather than accepting arbitrary pixels.
    visual_target.target_offset({'found': True, 'bbox_1000': box}, frame)
    box = np.asarray(box, dtype=float) * [width / 1000, height / 1000, width / 1000, height / 1000]
    left, top, right, bottom = box.tolist()
    polygon = _points(detected.get('destination_polygon_1000'), width, height)
    if np.any(polygon[:, 0, 0] <= 2) or np.any(polygon[:, 0, 0] >= width - 3) or np.any(polygon[:, 0, 1] <= 2) or np.any(polygon[:, 0, 1] >= height - 3):
        raise TargetUnlocalized('delivery_boundary_truncated')
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    region = np.zeros((height, width), np.uint8)
    cv2.fillPoly(region, [polygon], 1)
    samples = region.copy()
    samples[max(0, int(top)):min(height, math.ceil(bottom)),
            max(0, int(left)):min(width, math.ceil(right))] = 0
    saturated = (samples != 0) & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 40)
    if np.count_nonzero(saturated) < max(100, width * height * .001):
        raise TargetUnlocalized('delivery_color_unverified')
    histogram = np.bincount(hsv[:, :, 0][saturated], minlength=180)
    hue = int(np.argmax(histogram))
    difference = np.abs(hsv[:, :, 0].astype(float) - hue)
    hue_match = np.minimum(difference, 180 - difference) <= 12
    selected = saturated & hue_match
    if np.count_nonzero(selected) < .55 * np.count_nonzero(samples):
        raise TargetUnlocalized('delivery_color_not_uniform')
    signature = [hue, int(np.median(hsv[:, :, 1][selected])), int(np.median(hsv[:, :, 2][selected]))]
    if expected_rgb is not None:
        signal = cv2.cvtColor(np.asarray([[expected_rgb]], dtype=np.uint8), cv2.COLOR_RGB2HSV)[0, 0]
        hue_error = abs(float(signal[0]) - hue)
        if signal[1] < 65 or min(hue_error, 180 - hue_error) > 12:
            raise TargetUnlocalized('delivery_zone_signal_mismatch')
    if expected_hsv is not None:
        if not isinstance(expected_hsv, list) or len(expected_hsv) != 3:
            raise TargetUnlocalized('delivery_reference_invalid')
        hue_error = abs(float(expected_hsv[0]) - hue)
        if min(hue_error, 180 - hue_error) > 12:
            raise TargetUnlocalized('delivery_zone_changed')
    actual = ((region != 0) & hue_match & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 40)).astype(np.uint8)
    contours, _ = cv2.findContours(actual, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contours = [contour for contour in contours if cv2.contourArea(contour) >= width * height * .003]
    if not contours:
        raise TargetUnlocalized('delivery_region_not_connected')
    contour = max(contours, key=cv2.contourArea)
    # A conservative completion proof: the entire cargo image bbox, including
    # its visible floor contact, is enclosed by the same real color region's
    # outside boundary AND the fully visible floor-zone polygon. A partially
    # hidden boundary or mere overlap can never become a delivery result.
    corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
    inside = all(cv2.pointPolygonTest(shape, point, True) >= 3
                 for shape in (polygon, contour) for point in corners)
    center = np.mean(polygon[:, 0, :], axis=0)
    return {'zone_color_hsv': signature,
            'zone_polygon_1000': detected['destination_polygon_1000'],
            'zone_pixel_contour': contour[:, 0, :].tolist(),
            'zone_center_1000': [float(center[0] * 1000 / width), float(center[1] * 1000 / height)],
            'cargo_bbox_1000': detected['cargo_bbox_1000'], 'contained': inside}


async def observe_cargo(cargo: str, destination: str, *, after_frame_id=None,
                        after_received_at=None, expected_hsv=None, expected_rgb=None) -> dict:
    """Observe both exact targets on one stationary, sensor-paired image."""
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
        '只定位本张图像中指定的同一货物和指定颜色的地面配送区域，不判断送达或动作成功。'
        '输出JSON：{"cargo_count":整数,"destination_count":整数,"cargo_ground_contact_visible":布尔值,'
        '"destination_is_floor":布尔值,"destination_boundary_complete":布尔值,'
        '"cargo_bbox_1000":[左,上,右,下],"destination_polygon_1000":[[x,y],...]}。'
        '坐标相对整张图归一化0到1000；配送区域多边形3到12个顶点，沿真实可见边界顺序排列。'
        'cargo_count和destination_count必须分别是符合完整描述的目标数量，有歧义或相似多个不能选一个冒充唯一。'
        '只接受确在地面的货物以及指定的有色地面区域；同色杂物、墙面或箱子不能当作配送区域。'
        '看不清完整区域外边界时destination_boundary_complete=false，不能补画被遮挡、画面外或想象的边界。'
        'cargo_ground_contact_visible仅在货物与地面接触处真实可见时为true。'
        '不输出世界坐标、距离或送达结论。')
    detected = await visual_target._ask_vision(jpeg,
        '货物：' + cargo + '；本单唯一配送区域：' + destination,
        frame, instruction, 'cargo_delivery_geometry')
    if (type(detected.get('cargo_count')) is not int or detected.get('cargo_count') != 1
            or type(detected.get('destination_count')) is not int or detected.get('destination_count') != 1
            or detected.get('cargo_ground_contact_visible') is not True
            or detected.get('destination_is_floor') is not True
            or detected.get('destination_boundary_complete') is not True):
        raise TargetUnlocalized('cargo_or_destination_ambiguous')
    geometry = await invoke_blocking(_zone_geometry, jpeg, frame, detected, expected_hsv, expected_rgb)
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
