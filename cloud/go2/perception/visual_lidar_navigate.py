"""Navigate to a visible object using paired RGB, LiDAR and the live odom map."""
from __future__ import annotations

import asyncio
import contextlib
import math
import time

import numpy as np

from cloud.go2.connection import go2
from cloud.go2.control import control_lease
from cloud.go2.execution import current_context
from cloud.go2.navigation.navigator import ARRIVAL_THRESHOLD, navigator
from cloud.go2.navigation.occupancy_grid import (
    OccupancyGrid, ROBOT_HALF_LENGTH_M, SAFETY_MARGIN_M,
)
from cloud.go2.perception.rgb_lidar import (
    APPROACH_STOP_RADIUS_M, bbox_mask, nominal_camera, object_returns_in_box,
    project_odom_points, save_paired_sample, select_reachable_stop,
)
from cloud.go2.perception.visual_approach import _save_diagnostic_frame, target_offset
from cloud.go2.perception.visual_target import TargetUnlocalized, detect_visible_target
from cloud.observability import emit_agent_event

STOP_SPEED_M_S = 0.30  # Match the existing navigator speed; diagonal joystick axes split the command.
TARGET_CLEARANCE_M = ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M
VISUAL_CENTER_OFFSET = 0.15

_REASONS = {
    "invalid_target": "目标描述无效",
    "disconnected": "机器狗未连接",
    "posture_not_confirmed": "机器狗尚未确认站立",
    "frame_unavailable": "相机画面不可用",
    "target_not_visible": "画面中未看到目标",
    "vision_result_invalid": "目标区域无法解析",
    "voxel_unavailable": "雷达点云不可用",
    "frame_mismatch": "相机、雷达和地图坐标系不一致",
    "pose_3d_unavailable": "缺少相机采集时的三维姿态",
    "object_returns_unavailable": "目标区域内没有可定位的雷达物体点",
    "stop_unreachable": "地图中找不到目标附近可达停靠点",
    "arrival_target_unconfirmed": "抵达停靠点后未能重新确认目标",
    "target_not_centered": "目标仍偏离画面中央，当前地图中没有可继续靠近的停靠点",
}


def _paired_target_xy(frame: dict, voxel: dict, voxel_at: float | None,
                      bbox: list) -> tuple[tuple[float, float], OccupancyGrid, dict]:
    """Locate obstacle-height returns inside the visual box from one sensor pair."""
    pose = frame.get("pose_at_capture") or {}
    data = voxel.get("data") or {}
    voxel_frame = (data.get("header") or {}).get("frame_id") or data.get("frame_id")
    if voxel_frame != "odom" or not frame.get("odom_session_id") or pose.get("session_id") != frame["odom_session_id"]:
        raise TargetUnlocalized("frame_mismatch")
    if pose.get("z") is None or not pose.get("orientation"):
        raise TargetUnlocalized("pose_3d_unavailable")
    skew = (voxel_at - frame["received_at"]
            if isinstance(voxel_at, (int, float)) else None)
    try:
        points = np.asarray((data.get("data") or {}).get("points"), dtype=float)
        if points.ndim != 2 or points.shape[1] < 3 or not len(points):
            raise ValueError("point_shape_invalid")
        grid = OccupancyGrid(voxel, robot_pose=pose)
        pixels, depth, world = project_odom_points(
            points, pose, nominal_camera(frame["width"], frame["height"]),
            frame["width"], frame["height"])
        in_box = bbox_mask(pixels, bbox, frame["width"], frame["height"])
        object_mask = object_returns_in_box(world, in_box, grid.measured_floor_z)
    except (ValueError, TypeError, KeyError) as exc:
        raise TargetUnlocalized("voxel_unavailable") from exc
    if not object_mask.any():
        emit_agent_event("vision.lidar", phase="target_localization", passed=False,
                         frame_id=frame.get("frame_id"), points_in_box=int(in_box.sum()),
                         floor_z_m=grid.measured_floor_z,
                         reason="object_returns_unavailable")
        raise TargetUnlocalized("object_returns_unavailable")
    target = tuple(float(v) for v in np.median(world[object_mask, :2], axis=0))
    emit_agent_event("vision.lidar", phase="target_localization", passed=True,
                     frame_id=frame.get("frame_id"), camera_model="provisional_URDF_hfov_90deg",
                     voxel_skew_s=round(skew, 3) if skew is not None else None,
                     points_in_box=int(in_box.sum()),
                     object_returns=int(object_mask.sum()), floor_z_m=grid.measured_floor_z,
                     object_depth_median_m=round(float(np.median(depth[object_mask])), 3),
                     target_odom_xy=[round(v, 3) for v in target])
    return target, grid, pose


async def _locate(target: str) -> tuple[tuple[float, float], dict, dict, float]:
    captured = go2.latest_frame_snapshot()
    if captured is None:
        raise TargetUnlocalized("frame_unavailable")
    jpeg, frame = captured
    session_id = getattr(go2, "_odom_session_id", None)
    if not session_id or frame.get("odom_session_id") != session_id:
        raise TargetUnlocalized("frame_mismatch")
    voxel_before, voxel_before_at = go2.voxel_raw, go2.voxel_received_at
    image_path = _save_diagnostic_frame(jpeg, frame)
    observation = await detect_visible_target(jpeg, target, frame)
    offset, _ = target_offset(observation, frame)
    bbox = observation["bbox_1000"]
    live, reason = navigator._snapshot()
    if reason or getattr(go2, "_odom_session_id", None) != session_id:
        raise TargetUnlocalized(reason or "frame_mismatch")
    current_pose, current_grid = live
    voxel_after, voxel_after_at = go2.voxel_raw, go2.voxel_received_at
    frame_at = frame.get("received_at")
    options = [(voxel, at, source) for voxel, at, source in (
        (voxel_before, voxel_before_at, "at_frame"),
        (voxel_after, voxel_after_at, "after_detection"))
        if isinstance(voxel, dict) and isinstance(at, (int, float))]
    if not options:
        raise TargetUnlocalized("voxel_unavailable")
    voxel, voxel_at, source = min(
        options, key=lambda item: abs(item[1] - frame_at)
        if isinstance(frame_at, (int, float)) else 0)
    emit_agent_event("vision.lidar", phase="sensor_pair", frame_id=frame.get("frame_id"),
                     source=source,
                     frame_age_s=round(time.monotonic() - frame_at, 3)
                     if isinstance(frame_at, (int, float)) else None,
                     voxel_skew_s=round(voxel_at - frame_at, 3)
                     if isinstance(frame_at, (int, float)) else None)
    target_xy, _, _ = _paired_target_xy(frame, voxel, voxel_at, bbox)
    save_paired_sample(image_path, frame, voxel, voxel_at, bbox)
    return target_xy, current_pose, current_grid, offset


def _visual_heading_bias(target_xy: tuple[float, float], pose: dict,
                         offset: float) -> float:
    """Correct the map bearing with the observed horizontal camera bearing."""
    camera_error = -math.atan(offset * math.tan(math.radians(45)))
    map_error = math.atan2(target_xy[1] - pose["y"],
                           target_xy[0] - pose["x"]) - pose["heading"]
    return math.atan2(math.sin(camera_error - map_error),
                      math.cos(camera_error - map_error))


async def _update_visual_heading_while_moving(target: str,
                                               target_xy: tuple[float, float],
                                               guidance: dict) -> None:
    """Observe the target in parallel with map navigation, without pausing motion."""
    while True:
        await asyncio.sleep(0.4)
        captured = go2.latest_frame_snapshot()
        if captured is None:
            continue
        jpeg, frame = captured
        pose = frame.get("pose_at_capture") or {}
        if (frame.get("odom_session_id") != getattr(go2, "_odom_session_id", None)
                or pose.get("session_id") != frame.get("odom_session_id")):
            continue
        try:
            observation = await detect_visible_target(jpeg, target, frame)
            offset, _ = target_offset(observation, frame)
            bias = _visual_heading_bias(target_xy, pose, offset)
        except (TargetUnlocalized, KeyError, TypeError, ValueError):
            continue
        guidance["bias"] = bias
        guidance["offset"] = offset
        emit_agent_event("vision.motion", phase="visual_heading_update",
                         frame_id=frame.get("frame_id"), image_offset=round(offset, 3),
                         heading_bias_rad=round(bias, 3))


async def navigate_to_visible_target(target: str) -> dict:
    """Follow the measured map to a visible object's free stop cell and reobserve."""
    if not isinstance(target, str) or not target.strip() or len(target) > 100:
        return {"result": "blocked", "reason": "invalid_target", "message": _REASONS["invalid_target"]}
    if not go2.is_connected:
        return {"result": "blocked", "reason": "disconnected", "message": _REASONS["disconnected"]}
    if go2.fsm_state != "standing":
        return {"result": "blocked", "reason": "posture_not_confirmed", "message": _REASONS["posture_not_confirmed"]}
    context = current_context()
    navigation = None
    try:
        control_lease.acquire(context)
        target_xy, pose, grid, offset = await _locate(target)
        stop = select_reachable_stop(grid, pose, target_xy)
        if stop is None:
            raise TargetUnlocalized("stop_unreachable")
        bias = _visual_heading_bias(target_xy, pose, offset)
        guidance = {"bias": bias, "offset": offset}
        emit_agent_event("vision.gate", gate="mapped_stop", passed=True,
                         target_odom_xy=[round(v, 3) for v in target_xy], stop=stop,
                         image_offset=round(offset, 3), heading_bias_rad=round(bias, 3),
                         odom_session=pose.get("session_id"))
        if not stop["already_near"]:
            emit_agent_event("vision.motion", phase="request", kind="rgb_lidar_navigation",
                             target_odom_xy=[round(v, 3) for v in target_xy],
                             stop={key: stop[key] for key in ("x", "y")},
                             speed_limit=STOP_SPEED_M_S,
                             keepout_radius_m=TARGET_CLEARANCE_M,
                             path_following="continuous_map_navigation_with_visual_heading")
            visual_task = asyncio.create_task(
                _update_visual_heading_while_moving(target, target_xy, guidance))
            try:
                navigation = await navigator.go_to_coordinate(
                    stop["x"], stop["y"], owner_session_id=context.session_id,
                    keepout=(target_xy[0], target_xy[1], TARGET_CLEARANCE_M),
                    speed_limit=STOP_SPEED_M_S, holonomic=True,
                    face_target=target_xy,
                    face_target_bias_rad=lambda: guidance["bias"],
                    stop_when_near_target_m=APPROACH_STOP_RADIUS_M)
            finally:
                visual_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await visual_task
            emit_agent_event("vision.motion", phase="result", kind="rgb_lidar_navigation",
                             status=navigation.get("status"), reason=navigation.get("reason"),
                             task_id=navigation.get("task_id"),
                             last_trusted_pose=navigation.get("last_trusted_pose"),
                             diagnostics=navigation.get("diagnostics"))
            if navigation.get("status") != "succeeded":
                status = navigation.get("status")
                return {"result": "cancelled" if status == "cancelled" else "blocked" if status == "blocked" else "error",
                        "reason": navigation.get("reason"), "target": target,
                        "navigation": navigation,
                        "message": f"前往「{target}」时已停止：{navigation.get('message') or navigation.get('reason')}"}
        try:
            final_xy, final_pose, _, final_offset = await _locate(target)
        except TargetUnlocalized as exc:
            raise TargetUnlocalized("arrival_target_unconfirmed") from exc
        distance = math.hypot(final_pose["x"] - final_xy[0], final_pose["y"] - final_xy[1])
        if distance > APPROACH_STOP_RADIUS_M + ARRIVAL_THRESHOLD / 2:
            raise TargetUnlocalized("arrival_target_unconfirmed")
        if abs(final_offset) > VISUAL_CENTER_OFFSET:
            raise TargetUnlocalized("target_not_centered")
        return {"result": "ok", "target": target,
                "confirmation": "odom_stopped_and_rgb_lidar_reobserved",
                "target_odom_xy": [round(v, 3) for v in final_xy],
                "distance_to_target_m": round(distance, 2),
                "target_image_offset": round(final_offset, 3),
                "stop": stop, "navigation": navigation,
                "message": f"已到「{target}」附近，目标在画面中央，雷达测距约 {distance:.2f} 米。"}
    except TargetUnlocalized as exc:
        return {"result": "blocked", "reason": exc.reason, "target": target,
                "navigation": navigation,
                "message": f"前往「{target}」时已停止：{_REASONS.get(exc.reason, exc.reason)}。"}
