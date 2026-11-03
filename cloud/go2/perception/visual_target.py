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
from cloud.observability import emit_agent_event


CALIBRATION_PATH = Path(__file__).with_name('camera_calibration.json')
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


async def _ask_vision(jpeg: bytes, target: str, frame: dict,
                      instruction: str, mode: str) -> dict:
    """Keep both visual tools on the same JSON request and diagnostic path."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from cloud.go2.models import get_vision_llm
    width, height = frame.get('width'), frame.get('height')
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        emit_agent_event('vision.gate', gate='frame_dimensions', passed=False,
                         frame_id=frame.get('frame_id'), width=width, height=height,
                         reason='vision_result_invalid')
        raise TargetUnlocalized('vision_result_invalid')
    emit_agent_event('vision.model', phase='request', frame_id=frame.get('frame_id'),
                     width=width, height=height, mode=mode)
    started = time.monotonic()
    try:
        response = await get_vision_llm().bind(response_format={'type': 'json_object'}).ainvoke([
            SystemMessage(content=instruction),
            HumanMessage(content=[{'type': 'text', 'text': '目标：' + target},
                                  {'type': 'image_url', 'image_url': {'url':
                                   'data:image/jpeg;base64,' + base64.b64encode(jpeg).decode()}}]),
        ])
    except Exception as exc:
        emit_agent_event('vision.model', phase='error', frame_id=frame.get('frame_id'),
                         mode=mode, error_type=type(exc).__name__, detail=str(exc)[:300],
                         duration_ms=round((time.monotonic() - started) * 1000))
        raise
    raw = getattr(response, 'content', None)
    # Keep the model's textual answer for diagnosis, never the image bytes.
    emit_agent_event('vision.model', phase='response', frame_id=frame.get('frame_id'),
                     mode=mode,
                     output=raw[:4096] if isinstance(raw, str) else str(raw)[:4096],
                     output_length=len(raw) if hasattr(raw, '__len__') else None,
                     output_type=type(raw).__name__,
                     duration_ms=round((time.monotonic() - started) * 1000),
                     frame_age_at_response_s=(round(time.monotonic() - frame['received_at'], 3)
                                              if isinstance(frame.get('received_at'), (int, float))
                                              else None))
    try:
        if not isinstance(raw, str):
            raise ValueError('non-text vision output')
        observation = json.loads(raw)
        if not isinstance(observation, dict):
            raise ValueError('non-object vision output')
        fields = (('found', 'bbox_1000') if mode == 'visible_target' else
                  ('count', 'static', 'confidence', 'bbox',
                   'ground_contact_visible', 'contact_pixel'))
        emit_agent_event('vision.model', phase='parsed', frame_id=frame.get('frame_id'),
                         mode=mode,
                         observation={key: observation.get(key) for key in fields})
        return observation
    except (TypeError, ValueError) as exc:
        emit_agent_event('vision.gate', gate='json_response', passed=False,
                         frame_id=frame.get('frame_id'), mode=mode,
                         reason='vision_result_invalid',
                         detail=str(exc))
        raise TargetUnlocalized('vision_result_invalid') from exc


async def detect_visible_target(jpeg: bytes, target: str, frame: dict) -> dict:
    """Find any matching visible object and its image region for visual approach."""
    instruction = (
        '在图像中寻找用户描述的物体。'
        '只输出 JSON：{"found":布尔值,"bbox_1000":[左,上,右,下]或null}。'
        '画面里有任何一个符合描述的物体就设 found=true；若有多个，选画面中最清楚的一个。'
        '看不到符合描述的物体时设 found=false、bbox_1000=null。'
        'bbox_1000 必须用相对于整张图的 0–1000 坐标，左上角为原点，'
        '满足 0<=左<右<=1000、0<=上<下<=1000。不要输出像素坐标。'
        '不要估计距离或世界坐标，不要输出 JSON 之外的文字。'
    )
    return await _ask_vision(jpeg, target, frame, instruction, 'visible_target')


async def detect_ground_target(jpeg: bytes, target: str, frame: dict) -> dict:
    """Ask vision for pixels needed by the calibrated ground-contact route."""
    width, height = frame.get('width'), frame.get('height')
    instruction = (
        f'只分析给定的 {width}x{height} 像素图像中指定目标。输出纯 JSON：'
        '{"count":整数,"confidence":0到1,"static":布尔值,'
        '"ground_contact_visible":布尔值,"contact_pixel":[像素x,像素y],'
        '"bbox":[左,上,右,下]}。'
        'count 是与描述匹配的目标数量。若目标不唯一、地面接触点不可见或目标在桌面上，'
        'ground_contact_visible 必须为 false。无法确认目标时设 count=0、confidence=0、static=false、'
        'ground_contact_visible=false、contact_pixel=null、bbox=null。'
        f'bbox 必须是当前图像的绝对像素坐标，左上角为原点，满足 0<=左<右<={width}、'
        f'0<=上<下<={height}；不可输出 0~1 或 0~1000 归一化坐标。'
        '禁止估计距离或世界坐标，也不要输出 JSON 之外的文字。'
    )
    return await _ask_vision(jpeg, target, frame, instruction, 'ground_contact')
