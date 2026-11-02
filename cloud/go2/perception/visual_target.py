"""Conservative visual ground-target localization for Go2.

The calibration file is deliberately absent from the repository. It must be
produced by physical calibration and checked against measured replay samples.
"""
from __future__ import annotations

import json
import base64
import math
import time
from pathlib import Path

from cloud.go2.navigation.astar import astar

CALIBRATION_PATH = Path(__file__).with_name('camera_calibration.json')
AVOIDANCE_EVIDENCE_PATH = Path(__file__).with_name('avoidance_evidence.json')
MAX_SAMPLE_SKEW = 0.25
MAX_FRAME_AGE = 0.8
MAX_POSITION_ERROR_M = 0.20
STANDOFF_M = 1.55
STANDOFF_TOLERANCE_M = 0.15
TARGET_KEEPOUT_M = 0.80


class TargetUnlocalized(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError('site evidence failed validation')


def require_avoidance_evidence(serial: str | None, path: Path = AVOIDANCE_EVIDENCE_PATH) -> dict:
    """Require an on-site record for the exact robot and Navigator motion channel."""
    try:
        data = json.loads(path.read_text())
        _require(bool(serial and data['robot_serial'] == serial))
        _require(data['connection_method'] == 'Remote')
        _require(data['motion_channel'] == 'WIRELESS_CONTROLLER')
        _require(data['switch_get_enabled'] is True)
        _require(data['motion_channel_obstacle_test_passed'] is True)
        _require(bool(data['firmware_version'] and data['tested_at']))
        return data
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise TargetUnlocalized('avoidance_unverified') from exc


def load_calibration(path: Path = CALIBRATION_PATH, *, serial: str | None = None) -> dict:
    try:
        data = json.loads(path.read_text())
        _require(bool(serial and data['robot_serial'] == serial))
        _require(data['measured_on_robot'] is True)
        _require(data['replay_verified'] is True)
        _require(bool(data['camera_id'] and data['odom_session_independent'] is True))
        _require(all(math.isfinite(float(data[k])) and float(data[k]) > 0
                     for k in ('fx', 'fy', 'reprojection_error_px', 'position_error_m',
                               'camera_height_above_floor_m')))
        _require(all(math.isfinite(float(data[k])) for k in ('cx', 'cy')))
        _require(data['posture'] == 'standing')
        _require(len(data['rotation_body_from_camera']) == 3)
        _require(all(len(row) == 3 and all(math.isfinite(float(v)) for v in row)
                     for row in data['rotation_body_from_camera']))
        rows = data['rotation_body_from_camera']
        _require(all(abs(sum(float(rows[i][k]) * float(rows[j][k]) for k in range(3))
                         - (1 if i == j else 0)) < 0.02
                     for i in range(3) for j in range(3)))
        _require(len(data['translation_body_from_camera_m']) == 3)
        _require(all(math.isfinite(float(v)) for v in data['translation_body_from_camera_m']))
        _require(float(data['position_error_m']) <= MAX_POSITION_ERROR_M)
        return data
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise TargetUnlocalized('calibration_unavailable') from exc


def validate_samples(frame: dict, pose: dict, pose_at: float | None,
                     voxel_at: float | None, pose_frame: str | None,
                     voxel_frame: str | None, session_id: str | None) -> None:
    now = time.monotonic()
    sampled = frame.get('received_at')
    if not isinstance(sampled, (int, float)) or now - sampled > MAX_FRAME_AGE or sampled > now:
        raise TargetUnlocalized('frame_stale')
    if (pose_at is None or voxel_at is None or abs(sampled - pose_at) > MAX_SAMPLE_SKEW
            or abs(sampled - voxel_at) > MAX_SAMPLE_SKEW):
        raise TargetUnlocalized('sensor_frame_skew')
    if pose_frame != 'odom' or voxel_frame != 'odom' or not session_id or pose.get('session_id') != session_id:
        raise TargetUnlocalized('odom_session_mismatch')
    if frame.get('odom_session_id') != session_id:
        raise TargetUnlocalized('video_session_mismatch')
    if not all(isinstance(pose.get(k), (float, int)) and math.isfinite(pose[k])
               for k in ('x', 'y', 'heading')):
        raise TargetUnlocalized('pose_invalid')


def project_ground_contact(observation: dict, frame: dict, pose: dict,
                           floor_z: float, calibration: dict) -> dict:
    """Intersect a measured contact pixel ray with the measured odom floor."""
    try:
        confidence = float(observation.get('confidence', 0))
        u, v = observation.get('contact_pixel', (None, None))
    except (TypeError, ValueError) as exc:
        raise TargetUnlocalized('vision_result_invalid') from exc
    if (observation.get('count') != 1 or observation.get('ground_contact_visible') is not True
            or observation.get('static') is not True or not math.isfinite(confidence)
            or confidence < 0.85):
        raise TargetUnlocalized('target_ambiguous')
    width, height = frame['width'], frame['height']
    if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (u, v)) or not (0 <= u < width and 0 <= v < height):
        raise TargetUnlocalized('contact_point_invalid')
    ray = ((u - float(calibration['cx'])) / float(calibration['fx']),
           (v - float(calibration['cy'])) / float(calibration['fy']), 1.0)
    matrix = calibration['rotation_body_from_camera']
    direction = [sum(float(matrix[i][j]) * ray[j] for j in range(3)) for i in range(3)]
    origin = [float(x) for x in calibration['translation_body_from_camera_m']]
    if direction[2] >= -0.05 or not math.isfinite(floor_z):
        raise TargetUnlocalized('ground_ray_unreliable')
    # Camera height is measured above the floor in the calibrated standing
    # posture. The voxel floor_z is in odom and is only evidence that a floor
    # was measured; it cannot be subtracted from a body-frame translation.
    distance = -float(calibration['camera_height_above_floor_m']) / direction[2]
    if not 0.3 <= distance <= 4.0:
        raise TargetUnlocalized('ground_intersection_unreliable')
    bx, by = origin[0] + distance * direction[0], origin[1] + distance * direction[1]
    yaw = pose['heading']
    x = pose['x'] + math.cos(yaw) * bx - math.sin(yaw) * by
    y = pose['y'] + math.sin(yaw) * bx + math.cos(yaw) * by
    uncertainty = float(calibration['position_error_m']) + abs(distance) * float(calibration['reprojection_error_px']) / min(float(calibration['fx']), float(calibration['fy']))
    if uncertainty > MAX_POSITION_ERROR_M:
        raise TargetUnlocalized('position_uncertainty_excessive')
    return {'frame_id': 'odom', 'x': x, 'y': y, 'uncertainty_m': uncertainty,
            'odom_session_id': pose['session_id'], 'source_frame_id': frame['frame_id'],
            'source_ts': frame['captured_at'], 'bbox': observation.get('bbox')}


def select_standoff(grid, pose: dict, target: dict) -> dict:
    """Choose a reachable known-free cell outside the target's safety radius."""
    if target['frame_id'] != grid.frame_id or target['odom_session_id'] != pose.get('session_id'):
        raise TargetUnlocalized('odom_session_mismatch')
    try:
        start = grid.odom_to_grid(pose['x'], pose['y'])
        contact = grid.odom_to_grid(target['x'], target['y'])
    except ValueError as exc:
        raise TargetUnlocalized('target_outside_local_map') from exc
    if not grid.is_free(*start):
        raise TargetUnlocalized('start_not_free')
    # The projected contact must agree with observed local floor evidence.
    # An unknown patch, even if another standoff cell exists, is insufficient.
    floor_near_contact = False
    contact_radius = math.ceil(0.20 / grid.resolution)
    for gy in range(contact[1] - contact_radius, contact[1] + contact_radius + 1):
        for gx in range(contact[0] - contact_radius, contact[0] + contact_radius + 1):
            if grid.is_free(gx, gy):
                x, y = grid.grid_to_odom(gx, gy)
                if math.hypot(x - target['x'], y - target['y']) <= 0.20:
                    floor_near_contact = True
                    break
        if floor_near_contact:
            break
    if not floor_near_contact:
        raise TargetUnlocalized('target_ground_unverified')
    blocked = grid.grid.copy()
    keepout_radius = TARGET_KEEPOUT_M + target['uncertainty_m']
    for gy in range(blocked.shape[0]):
        for gx in range(blocked.shape[1]):
            x, y = grid.grid_to_odom(gx, gy)
            if math.hypot(x - target['x'], y - target['y']) < keepout_radius:
                blocked[gy, gx] = True
    if blocked[start[1], start[0]]:
        raise TargetUnlocalized('start_inside_target_keepout')
    candidates = []
    radius = math.ceil((STANDOFF_M + STANDOFF_TOLERANCE_M) / grid.resolution)
    tx, ty = grid.odom_to_grid(target['x'], target['y'])
    for gy in range(ty - radius, ty + radius + 1):
        for gx in range(tx - radius, tx + radius + 1):
            if not grid.is_free(gx, gy) or blocked[gy, gx]:
                continue
            x, y = grid.grid_to_odom(gx, gy)
            distance = math.hypot(x - target['x'], y - target['y'])
            if not STANDOFF_M - STANDOFF_TOLERANCE_M <= distance <= STANDOFF_M + STANDOFF_TOLERANCE_M:
                continue
            heading = math.atan2(target['y'] - y, target['x'] - x)
            if not grid.is_body_clear(x, y, heading):
                continue
            path = astar(blocked, start, (gx, gy))
            if path:
                candidates.append((abs(distance - STANDOFF_M), len(path), x, y, distance, heading))
    if not candidates:
        raise TargetUnlocalized('no_known_free_standoff')
    _, _, x, y, distance, heading = min(candidates)
    return {'x': x, 'y': y, 'heading': heading, 'distance_to_target_m': distance}


async def detect_ground_target(jpeg: bytes, target: str) -> dict:
    """Ask vision for pixels only; never use model-supplied distance or coordinates."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from cloud.go2.models import get_vision_llm
    instruction = (
        '只分析给定图像中指定目标。输出纯 JSON：'
        '{"count":整数,"confidence":0到1,"static":布尔值,'
        '"ground_contact_visible":布尔值,"contact_pixel":[像素x,像素y],'
        '"bbox":[左,上,右,下]}。'
        'count 是与描述匹配的目标数量。若目标不唯一、地面接触点不可见或目标在桌面上，'
        'ground_contact_visible 必须为 false。禁止估计距离或世界坐标。'
    )
    response = await get_vision_llm().ainvoke([
        SystemMessage(content=instruction),
        HumanMessage(content=[{'type': 'text', 'text': '目标：' + target},
                              {'type': 'image_url', 'image_url': {'url':
                               'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()}}]),
    ])
    try:
        raw = response.content
        if not isinstance(raw, str):
            raise ValueError('non-text vision output')
        observation = json.loads(raw)
        if not isinstance(observation, dict):
            raise ValueError('non-object vision output')
        return observation
    except (TypeError, ValueError) as exc:
        raise TargetUnlocalized('vision_result_invalid') from exc
