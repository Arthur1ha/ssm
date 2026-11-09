"""Visual target association with measured LiDAR or calibrated floor geometry."""
from __future__ import annotations

import asyncio
from cloud.agents.common.runner import invoke_blocking
import hashlib
import logging
import os
import re
import uuid

import cv2
import numpy as np

import json
import base64
import math
import time
from pathlib import Path

from cloud.go2.navigation.astar import astar
from cloud.observability import current_event_context, emit_agent_event
from cloud.observability.logging_config import LOG_DIR
from cloud.go2.connection import go2
from cloud.space.execution import current_context


logger = logging.getLogger(__name__)
CAPTURE_DIR = LOG_DIR / "vision"
MAX_CAPTURE_BYTES = 2_000_000

CALIBRATION_PATH = Path(__file__).with_name('camera_calibration.json')
MAX_SAMPLE_SKEW = 0.25
MAX_FRAME_AGE = 0.8
MAX_POSITION_ERROR_M = 0.20
from cloud.go2.navigation.occupancy_grid import ROBOT_HALF_LENGTH_M, SAFETY_MARGIN_M

# Front edge to object contact; physical tolerance remains subject to site acceptance.
FRONT_STANDOFF_M = 1.20
STANDOFF_M = ROBOT_HALF_LENGTH_M + FRONT_STANDOFF_M
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
        _require(type(data['width']) is int and data['width'] > 0)
        _require(type(data['height']) is int and data['height'] > 0)
        _require(len(data['distortion_coefficients']) in (4, 5, 8, 12, 14))
        _require(all(math.isfinite(float(v)) for v in data['distortion_coefficients']))
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
    if type(sampled) not in (int, float) or not math.isfinite(sampled) or now - sampled > MAX_FRAME_AGE or sampled > now:
        raise TargetUnlocalized('frame_stale')
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in (pose_at, voxel_at))
            or abs(sampled - pose_at) > MAX_SAMPLE_SKEW
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
    if 'distortion_coefficients' in calibration:
        camera = np.array([[calibration['fx'], 0, calibration['cx']],
                           [0, calibration['fy'], calibration['cy']], [0, 0, 1]], dtype=float)
        undistort = (cv2.fisheye.undistortPoints if calibration.get('distortion_model') == 'equidistant'
                     else cv2.undistortPoints)
        pixel = undistort(np.array([[[u, v]]], dtype=float), camera,
                                    np.array(calibration['distortion_coefficients'], dtype=float), P=camera)
        u, v = pixel[0, 0]
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
    # Require actual nearby floor returns, not a walkable target cell: obstacle
    # inflation around a real toy correctly blocks walking onto that toy.
    floor_near_contact = False
    measured_floor = getattr(grid, 'measured_floor', None)
    contact_radius = math.ceil(0.20 / grid.resolution)
    for gy in range(contact[1] - contact_radius, contact[1] + contact_radius + 1):
        for gx in range(contact[0] - contact_radius, contact[0] + contact_radius + 1):
            if (measured_floor is not None and 0 <= gy < measured_floor.shape[0]
                    and 0 <= gx < measured_floor.shape[1] and measured_floor[gy, gx]):
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
        fields = (('scene_summary',) if mode == 'scene' else
                  ('found', 'bbox_1000', 'proximity', 'proximity_reason', 'scene_summary') if mode == 'visible_target' else
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
        '只输出 JSON：{"found":布尔值,"bbox_1000":[左,上,右,下]或null,'
        '"proximity":"nearby"或"far"或"uncertain","proximity_reason":"简短画面依据",'
        '"scene_summary":"用一句中文描述本张画面实际可见的主要物体和目标位置，不超过80字"}。'
        'scene_summary 无论是否找到目标都必须提供；只写当前画面可见事实，'
        '不推测画面外的物体，不提供行动建议。'
        '画面里有任何一个符合描述的物体就设 found=true；若有多个，选画面中最清楚的一个。'
        '看不到符合描述的物体时设 found=false、bbox_1000=null。'
        'bbox_1000 必须用相对于整张图的 0–1000 坐标，左上角为原点，'
        '满足 0<=左<右<=1000、0<=上<下<=1000。不要输出像素坐标。'
        '相机安装在机器狗前部。proximity 是找物任务的粗略视觉判断：'
        '目标已在机器狗身前近处、无需继续穿过一段房间才能靠近时为 nearby；'
        '仍在房间远处或明显需要继续靠近时为 far；遮挡、尺度或透视不足以判断时为 uncertain。'
        '结合地面、家具与整体场景判断，不要仅凭框大或位于画面底部判定 nearby。'
        '看不到目标时必须为 uncertain。此判断不表示可以触碰目标或已经精确到位。'
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


def save_diagnostic_frame(jpeg: bytes, frame: dict):
    """Save only frames used for a visual approach, outside the public app tree."""
    if not jpeg or len(jpeg) > MAX_CAPTURE_BYTES:
        emit_agent_event("vision.observe", phase="frame_capture", saved=False,
                         frame_id=frame.get("frame_id"), size_bytes=len(jpeg),
                         reason="capture_size_invalid")
        return None
    context = current_event_context()
    run_id = context.get("run_id") or uuid.uuid4().hex
    safe_run = re.sub(r"[^A-Za-z0-9_-]", "_", str(run_id))[:80]
    frame_id = frame.get("frame_id")
    safe_frame = str(frame_id) if type(frame_id) is int and frame_id >= 0 else "unknown"
    digest = hashlib.sha256(jpeg).hexdigest()
    path = CAPTURE_DIR / f"{safe_run}-{safe_frame}-{digest[:12]}.jpg"
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(jpeg)
            while view:
                written = os.write(fd, view)
                if written == 0:
                    raise OSError("frame capture write made no progress")
                view = view[written:]
        except OSError:
            path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    except OSError as exc:
        logger.warning("[Go2/VisualTarget] frame capture failed: %s", type(exc).__name__)
        emit_agent_event("vision.observe", phase="frame_capture", saved=False,
                         frame_id=frame_id, reason="capture_write_failed",
                         error_type=type(exc).__name__)
        return None
    emit_agent_event("vision.observe", phase="frame_capture", saved=True,
                     frame_id=frame_id, artifact=str(path),
                     sha256=digest, size_bytes=len(jpeg))
    return path


def target_offset(observation: dict, frame: dict) -> tuple[float, float]:
    """Return horizontal image error and apparent height, both normalized."""
    if observation.get("found") is not True:
        emit_agent_event("vision.gate", gate="target_visible", passed=False,
                         frame_id=frame.get("frame_id"), found=observation.get("found"),
                         reason="target_not_visible")
        raise TargetUnlocalized("target_not_visible")
    box = observation.get("bbox_1000")
    width, height = frame.get("width"), frame.get("height")
    if (not isinstance(box, list) or len(box) != 4
            or not isinstance(width, int) or not isinstance(height, int)
            or width <= 0 or height <= 0
            or not all(type(value) in (int, float) and math.isfinite(value) for value in box)):
        emit_agent_event("vision.gate", gate="bbox", passed=False,
                         frame_id=frame.get("frame_id"), bbox_1000=box,
                         width=width, height=height, detail="invalid_shape_or_type",
                         reason="vision_result_invalid")
        logger.warning('[Go2/VisualTarget] invalid normalized bbox shape: bbox=%r frame=%rx%r',
                       box, width, height)
        raise TargetUnlocalized("vision_result_invalid")
    left, top, right, bottom = box
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        emit_agent_event("vision.gate", gate="bbox", passed=False,
                         frame_id=frame.get("frame_id"), bbox_1000=box,
                         width=width, height=height, detail="outside_0_1000_or_degenerate",
                         reason="vision_result_invalid")
        logger.warning('[Go2/VisualTarget] bbox outside normalized 1000 range: bbox=%r frame=%rx%r',
                       box, width, height)
        raise TargetUnlocalized("vision_result_invalid")
    offset, apparent_height = (left + right - 1000) / 1000, (bottom - top) / 1000
    emit_agent_event("vision.gate", gate="target_geometry", passed=True,
                     frame_id=frame.get("frame_id"), bbox_1000=box,
                     bbox_pixels=[round(left * width / 1000, 1), round(top * height / 1000, 1),
                                  round(right * width / 1000, 1), round(bottom * height / 1000, 1)],
                     offset=round(offset, 4), apparent_height=round(apparent_height, 4))
    return offset, apparent_height



def _check_authorization() -> None:
    context = current_context()
    if context.expires_at and time.time() >= context.expires_at:
        raise TargetUnlocalized('authorization_expired')


def _live_pose(session_id: str) -> dict:
    _check_authorization()
    now = time.monotonic()
    pose = go2.odom
    stamp = go2.odom_received_at
    if not go2.is_connected:
        raise TargetUnlocalized('disconnected')
    if (not session_id or getattr(go2, '_odom_session_id', None) != session_id
            or pose.get('session_id') != session_id):
        raise TargetUnlocalized('odom_session_mismatch')
    if stamp is None or not 0 <= now - stamp <= MAX_FRAME_AGE:
        raise TargetUnlocalized('localization_unavailable')
    if not all(type(pose.get(k)) in (int, float) and math.isfinite(pose[k]) for k in ('x', 'y', 'heading')):
        raise TargetUnlocalized('pose_invalid')
    return pose


def _ensure_stable(reference: dict, current: dict) -> None:
    shift = math.hypot(current['x'] - reference['x'], current['y'] - reference['y'])
    turn = abs(math.atan2(math.sin(current['heading'] - reference['heading']),
                          math.cos(current['heading'] - reference['heading'])))
    if shift > 0.03 or turn > 0.04:
        raise TargetUnlocalized('pose_changed_during_detection')


def image_quality(jpeg: bytes) -> float:
    """A conservative sharpness gate; quality does not authorize free space."""
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return 0.0
    sharpness = float(cv2.Laplacian(image, cv2.CV_64F).var())
    return min(1.0, sharpness / 50.0)


async def _capture_still(*, after_frame_id=None, after_received_at=None):
    """Wait for a new received image after fresh odom proves a stationary pose."""
    _check_authorization()
    session_id = getattr(go2, '_odom_session_id', None)
    pose = _live_pose(session_id)
    first_at = go2.odom_received_at
    deadline = time.monotonic() + 3.0
    settled_at = None
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        current = _live_pose(session_id)
        _ensure_stable(pose, current)
        stamp = go2.odom_received_at
        if stamp > first_at and stamp - first_at >= 0.15 and settled_at is None:
            settled_at = time.monotonic()
        captured = go2.latest_frame_snapshot()
        if captured is None or settled_at is None:
            continue
        jpeg, frame = captured
        received_at = frame.get('received_at')
        if (frame.get('frame_id') == after_frame_id
                or not isinstance(received_at, (int, float))
                or received_at <= max(settled_at, after_received_at or 0)):
            continue
        frame_pose = frame.get('pose_at_capture') or {}
        validate_samples(frame, frame_pose, frame.get('pose_received_at'), received_at,
                         go2.odom_frame_id, 'odom', session_id)
        _ensure_stable(frame_pose, current)
        quality = await invoke_blocking(image_quality, jpeg)
        if quality < 0.5:
            raise TargetUnlocalized('image_quality_insufficient')
        return jpeg, frame, quality
    raise TargetUnlocalized('fresh_still_frame_unavailable')


async def _capture_view(*, after_frame_id=None, after_received_at=None):
    """Read a fresh camera view without requiring odometry or authorizing motion."""
    session = getattr(go2, '_odom_session_id', None)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        _check_authorization()
        if not go2.is_connected:
            raise TargetUnlocalized('disconnected')
        if getattr(go2, '_odom_session_id', None) != session:
            raise TargetUnlocalized('odom_session_mismatch')
        if getattr(go2, '_velocity_command_active', False):
            raise TargetUnlocalized('observation_requires_stop')
        captured = go2.latest_frame_snapshot()
        if captured is not None:
            jpeg, frame = captured
            stamp = frame.get('received_at')
            if (frame.get('frame_id') != after_frame_id
                    and type(stamp) in (int, float)
                    and 0 <= time.monotonic() - stamp <= MAX_FRAME_AGE
                    and stamp > (after_received_at or 0)
                    and frame.get('odom_session_id') == session):
                quality = await invoke_blocking(image_quality, jpeg)
                if quality < .5:
                    raise TargetUnlocalized('image_quality_insufficient')
                return jpeg, frame, quality
        await asyncio.sleep(.05)
    raise TargetUnlocalized('fresh_frame_unavailable')


async def observe_target(target: str, *, after_frame_id=None,
                         after_received_at=None, require_pose: bool = True) -> dict:
    """Detect on one fresh image; absence only describes this view.

    No image bytes or point clouds are returned, so the result fits a bounded
    Graph checkpoint. An empty target describes the scene for exploration.
    Search may inspect the camera before localization; such a view has no pose
    and cannot establish coverage or authorize a navigation target.
    """
    capture = _capture_still if require_pose else _capture_view
    jpeg, frame, quality = await capture(after_frame_id=after_frame_id,
                                         after_received_at=after_received_at)
    pose_reason = None
    if not require_pose:
        # Pairing belongs before model latency. A fresh submitted image will
        # naturally be older than MAX_FRAME_AGE when inference finishes.
        try:
            validate_samples(frame, frame.get('pose_at_capture') or {},
                             frame.get('pose_received_at'), frame['received_at'],
                             go2.odom_frame_id, 'odom', frame['odom_session_id'])
        except TargetUnlocalized as exc:
            if exc.reason not in {'sensor_frame_skew', 'pose_invalid', 'odom_session_mismatch'}:
                raise
            pose_reason = exc.reason
    # Preserve exactly the submitted frame even if model latency or a later
    # pose-stability gate rejects its result. Visitor images remain unpersisted.
    image_path = save_diagnostic_frame(jpeg, frame) if current_context().persist else None
    if target:
        observation = await detect_visible_target(jpeg, target, frame)
    else:
        scene = await _ask_vision(jpeg, "描述当前位置的场景", frame,
            '只输出 JSON：{"scene_summary":"用一句中文描述本张画面实际可见的主要物体、颜色或新鲜事物，不超过120字"}。'
            '不推测画面外的物体，不判断通行或到达，不提供行动建议。', 'scene')
        summary = scene.get('scene_summary')
        if not isinstance(summary, str) or not summary.strip():
            raise TargetUnlocalized('vision_result_invalid')
        observation = {'found': False, 'bbox_1000': None, 'scene_summary': summary.strip()[:240]}
    if type(observation.get('found')) is not bool:
        raise TargetUnlocalized('vision_result_invalid')
    if observation['found']:
        target_offset(observation, frame)
    pose = None
    try:
        pose = _live_pose(frame['odom_session_id'])
        if pose_reason is None:
            _ensure_stable(frame['pose_at_capture'], pose)
        else:
            pose = None
    except TargetUnlocalized as exc:
        if (require_pose or getattr(go2, '_odom_session_id', None) != frame['odom_session_id']
                or exc.reason not in {'localization_unavailable', 'pose_invalid',
                                      'sensor_frame_skew', 'odom_session_mismatch'}):
            raise
        # A sighting remains useful, but it has no geometric coverage or bearing.
        pose, pose_reason = None, exc.reason
    # Existing authenticated media endpoint, with a bounded retention count.
    from cloud.go2.activity_media import save_temporary_snapshot
    context = current_context()
    media_id = hashlib.sha256(context.session_id.encode()).hexdigest()[:24]
    media_url = save_temporary_snapshot(media_id, str(frame['frame_id']), jpeg)
    return {'found': observation['found'], 'frame': frame, 'pose': pose,
            'pose_reason': pose_reason,
            'observation': observation, 'quality': quality,
            'image_path': str(image_path) if image_path else None,
            'image_url': media_url, 'media_ref': media_url, 'frame_id': frame['frame_id'],
            'received_at': frame['received_at']}


async def locate_ground_target(target: str, calibration: dict, *,
                               after_frame_id=None, after_received_at=None) -> tuple[dict, dict, dict]:
    """Measured ground-contact projection cross-checked against a local cluster.

    Pairing gates run before model latency; afterwards the current pose and
    session must still agree. The caller obtains a fresh navigation map itself.
    """
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid
    from cloud.go2.perception.rgb_lidar import (
        associate_ground_target, quaternion_matrix, save_paired_sample,
    )
    state_at = getattr(go2, '_sport_state_observed_at', None)
    if (getattr(go2, 'body_state', None) != 'standing' or state_at is None
            or not 0 <= time.monotonic() - state_at <= 1.0):
        raise TargetUnlocalized('posture_not_confirmed')
    jpeg, frame, quality = await _capture_still(after_frame_id=after_frame_id,
                                               after_received_at=after_received_at)
    if (frame['width'], frame['height']) != (calibration['width'], calibration['height']):
        raise TargetUnlocalized('calibration_resolution_mismatch')
    pose = frame['pose_at_capture']
    voxel, voxel_at = go2.voxel_raw, go2.voxel_received_at
    validate_samples(frame, pose, frame.get('pose_received_at'), voxel_at,
                     go2.odom_frame_id, go2.voxel_frame_id, frame['odom_session_id'])
    if pose.get('z') is None or not pose.get('orientation'):
        raise TargetUnlocalized('pose_3d_unavailable')
    rotation = quaternion_matrix(pose['orientation'])
    # Site projection assumes the calibrated standing camera height. Reject
    # tilted terrain rather than silently treating pitch/roll as zero.
    if rotation[2, 2] < math.cos(0.05):
        raise TargetUnlocalized('posture_not_calibrated')
    grid = await invoke_blocking(OccupancyGrid, voxel, robot_pose=pose)
    if grid.measured_floor_z is None:
        raise TargetUnlocalized('measured_floor_unavailable')
    camera_height = pose['z'] + float(calibration['translation_body_from_camera_m'][2]) - grid.measured_floor_z
    if abs(camera_height - calibration['camera_height_above_floor_m']) > MAX_POSITION_ERROR_M:
        raise TargetUnlocalized('camera_floor_height_mismatch')
    observation = await detect_ground_target(jpeg, target, frame)
    live_pose = _live_pose(frame['odom_session_id'])
    _ensure_stable(pose, live_pose)
    location = project_ground_contact(observation, frame, pose, grid.measured_floor_z, calibration)
    box = observation.get('bbox')
    if not isinstance(box, list) or len(box) != 4:
        raise TargetUnlocalized('vision_result_invalid')
    bbox = [box[0] * 1000 / frame['width'], box[1] * 1000 / frame['height'],
            box[2] * 1000 / frame['width'], box[3] * 1000 / frame['height']]
    try:
        cluster = await invoke_blocking(associate_ground_target, voxel, pose, calibration,
                                          frame, bbox, grid.measured_floor_z, location)
    except (ValueError, TypeError, KeyError) as exc:
        raise TargetUnlocalized('object_returns_unavailable') from exc
    if cluster is None:
        raise TargetUnlocalized('target_lidar_disagreement')
    path = await invoke_blocking(save_diagnostic_frame, jpeg, frame) if current_context().persist else None
    if path is not None:
        await invoke_blocking(save_paired_sample, path, frame, voxel, voxel_at, bbox)
    live_pose = _live_pose(frame['odom_session_id'])
    _ensure_stable(pose, live_pose)
    location.update(image_path=str(path) if path else None, quality=quality,
                    source_received_at=frame['received_at'], lidar_points=cluster['points'])
    return location, live_pose, grid


async def locate_lidar_target(target: str, *, after_frame_id=None,
                              after_received_at=None) -> tuple[dict, dict, object]:
    """Pair a stationary visual detection with an isolated measured LiDAR object.

    The upstream camera profile is explicitly unverified on this robot. It only
    associates image pixels; range and world position come from real returns.
    Callers must independently confirm another frame before approaching.
    """
    from cloud.go2.navigation.occupancy_grid import OccupancyGrid
    from cloud.go2.perception.rgb_lidar import (
        associate_lidar_target, go2_camera_profile, image_bearing, quaternion_matrix,
        save_paired_sample,
    )
    state_at = getattr(go2, '_sport_state_observed_at', None)
    if (getattr(go2, 'body_state', None) != 'standing' or state_at is None
            or not 0 <= time.monotonic() - state_at <= 1.0):
        raise TargetUnlocalized('posture_not_confirmed')
    jpeg, frame, quality = await _capture_still(after_frame_id=after_frame_id,
                                               after_received_at=after_received_at)
    pose = frame['pose_at_capture']
    # Snapshot both cloud and receipt time before inference; never associate a
    # newer cloud obtained after model latency with this older image.
    voxel, voxel_at = go2.voxel_raw, go2.voxel_received_at
    validate_samples(frame, pose, frame.get('pose_received_at'), voxel_at,
                     go2.odom_frame_id, go2.voxel_frame_id, frame['odom_session_id'])
    if pose.get('z') is None or not pose.get('orientation'):
        raise TargetUnlocalized('pose_3d_unavailable')
    def ensure_stable_3d(current):
        _ensure_stable(pose, current)
        try:
            rotation = quaternion_matrix(pose['orientation']).T @ quaternion_matrix(current['orientation'])
            turn = math.acos(float(np.clip((np.trace(rotation)-1)/2, -1, 1)))
            if abs(current['z']-pose['z']) > .03 or turn > .04:
                raise TargetUnlocalized('pose_changed_during_detection')
        except (KeyError, TypeError) as exc:
            raise TargetUnlocalized('pose_3d_unavailable') from exc
    try:
        camera = go2_camera_profile(frame['width'], frame['height'])
    except ValueError as exc:
        raise TargetUnlocalized('camera_profile_resolution_mismatch') from exc
    path = await invoke_blocking(save_diagnostic_frame, jpeg, frame) if current_context().persist else None
    if path is not None:
        await invoke_blocking(save_paired_sample, path, frame, voxel, voxel_at)
    grid = await invoke_blocking(OccupancyGrid, voxel, robot_pose=pose)
    if grid.measured_floor_z is None:
        raise TargetUnlocalized('measured_floor_unavailable')
    observation = await detect_visible_target(jpeg, target, frame)
    target_offset(observation, frame)
    live_pose = _live_pose(frame['odom_session_id'])
    ensure_stable_3d(live_pose)
    bbox = observation['bbox_1000']
    association_diagnostics = {}
    try:
        cluster = await invoke_blocking(associate_lidar_target, voxel, pose, camera,
                                        frame, bbox, grid.measured_floor_z,
                                        diagnostics=association_diagnostics)
    except (ValueError, TypeError, KeyError) as exc:
        raise TargetUnlocalized('object_returns_unavailable') from exc
    emit_agent_event('vision.lidar', phase='foreground_association',
                     frame_id=frame['frame_id'], bbox_1000=bbox,
                     camera_model=camera['profile'], camera_source=camera['source_url'],
                     camera_measurement_status=camera['measurement_status'],
                     matched=cluster is not None, cluster=cluster,
                     association_diagnostics=association_diagnostics)
    if cluster is None:
        raise TargetUnlocalized('object_returns_unavailable')
    live_pose = _live_pose(frame['odom_session_id'])
    ensure_stable_3d(live_pose)
    location = dict(cluster, frame_id='odom', odom_session_id=frame['odom_session_id'],
                    source_frame_id=frame['frame_id'], source_received_at=frame['received_at'],
                    source_ts=frame.get('captured_at'), source='lidar_foreground_cluster',
                    camera_model=camera['profile'], camera_source=camera['source_url'],
                    camera_extrinsics_source=camera['extrinsics_source_url'],
                    camera_measurement_status=camera['measurement_status'],
                    bbox_1000=bbox, camera_width=frame['width'], camera_fx=camera['fx'],
                    image_bearing_rad=image_bearing(camera, bbox, frame['width'], frame['height']),
                    image_path=str(path) if path else None, quality=quality)
    return location, live_pose, grid
