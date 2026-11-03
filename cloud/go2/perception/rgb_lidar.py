"""Project Go2 metric LiDAR points into RGB frames for target navigation.

The current camera placement comes from the Go2 URDF and its 90-degree field
of view is provisional. Saved samples keep the timing offset for diagnosis.
"""
from __future__ import annotations

import json
import math
import os
from collections import deque
from pathlib import Path

import numpy as np

from cloud.observability import emit_agent_event
from cloud.go2.navigation.occupancy_grid import (
    OccupancyGrid, ROBOT_HALF_LENGTH_M, SAFETY_MARGIN_M, Z_FLOOR_M,
)

APPROACH_STOP_RADIUS_M = 2 * ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M + 0.15


def nominal_camera(width: int, height: int, hfov_deg: float = 90) -> dict:
    """Diagnostic-only URDF camera placement with an unverified pinhole FOV."""
    if not 20 <= hfov_deg <= 160:
        raise ValueError("diagnostic horizontal FOV must be between 20 and 160 degrees")
    focal = width / (2 * math.tan(math.radians(hfov_deg) / 2))
    return {
        "fx": focal, "fy": focal, "cx": width / 2, "cy": height / 2,
        "translation_body_from_camera_m": [0.32715, -0.00003, 0.04297],
        "rotation_body_from_camera": [[0, 0, 1], [-1, 0, 0], [0, -1, 0]],
    }


def _quaternion_matrix(values: list[float]) -> np.ndarray:
    q = np.asarray(values, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all():
        raise ValueError("invalid odom orientation")
    norm = float(np.linalg.norm(q))
    if norm < 0.5:
        raise ValueError("invalid odom orientation")
    x, y, z, w = q / norm
    return np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)],
    ])


def project_odom_points(points: np.ndarray, pose: dict, camera: dict,
                        width: int, height: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return pixels, camera depth, and matching odom points inside the image."""
    xyz = np.asarray(points, dtype=float)
    if xyz.ndim != 2 or xyz.shape[1] < 3:
        raise ValueError("expected N x 3 LiDAR points")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise ValueError("invalid image size")
    origin = np.asarray([pose.get(key) for key in ("x", "y", "z")], dtype=float)
    if not np.isfinite(origin).all():
        raise ValueError("3D odom pose unavailable")
    body_from_odom = _quaternion_matrix(pose["orientation"])
    camera_in_body = np.asarray(camera["translation_body_from_camera_m"], dtype=float)
    body_from_camera = np.asarray(camera["rotation_body_from_camera"], dtype=float)
    intrinsics = np.asarray([camera[key] for key in ("fx", "fy", "cx", "cy")], dtype=float)
    if (camera_in_body.shape != (3,) or body_from_camera.shape != (3, 3)
            or not np.isfinite(camera_in_body).all() or not np.isfinite(body_from_camera).all()
            or not np.isfinite(intrinsics).all() or np.any(intrinsics[:2] <= 0)
            or not np.allclose(body_from_camera.T @ body_from_camera, np.eye(3), atol=0.02)):
        raise ValueError("invalid camera calibration")
    finite = np.isfinite(xyz[:, :3]).all(axis=1)
    world_points = xyz[finite, :3]
    points_body = (world_points - origin) @ body_from_odom
    points_camera = (points_body - camera_in_body) @ body_from_camera
    in_front = points_camera[:, 2] > 0
    points_camera = points_camera[in_front]
    world_points = world_points[in_front]
    depth = points_camera[:, 2]
    pixels = np.column_stack((intrinsics[0] * points_camera[:, 0] / depth + intrinsics[2],
                              intrinsics[1] * points_camera[:, 1] / depth + intrinsics[3]))
    inside = ((pixels[:, 0] >= 0) & (pixels[:, 0] < width)
              & (pixels[:, 1] >= 0) & (pixels[:, 1] < height))
    return pixels[inside], depth[inside], world_points[inside]


def bbox_mask(pixels: np.ndarray, bbox_1000: list,
              width: int, height: int) -> np.ndarray:
    """Select projected pixels inside the target's normalized image box."""
    left, top, right, bottom = (float(value) for value in bbox_1000)
    if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
        raise ValueError("invalid target box")
    return ((pixels[:, 0] >= left * width / 1000)
            & (pixels[:, 0] <= right * width / 1000)
            & (pixels[:, 1] >= top * height / 1000)
            & (pixels[:, 1] <= bottom * height / 1000))


def bbox_depths(pixels: np.ndarray, depth: np.ndarray, bbox_1000: list,
                width: int, height: int) -> np.ndarray:
    """Collect projected LiDAR depths under one visual target box."""
    if len(pixels) != len(depth):
        raise ValueError("pixel and depth arrays differ")
    return depth[bbox_mask(pixels, bbox_1000, width, height)]


def object_returns_in_box(world_points: np.ndarray, in_box: np.ndarray,
                          measured_floor_z: float | None) -> np.ndarray:
    """Separate obstacle-height returns from floor returns in a visual box."""
    if len(world_points) != len(in_box):
        raise ValueError("point and box mask arrays differ")
    if measured_floor_z is None or not math.isfinite(measured_floor_z):
        return np.zeros(len(in_box), dtype=bool)
    return in_box & (world_points[:, 2] > measured_floor_z + Z_FLOOR_M)


def select_reachable_stop(grid, pose: dict, target_xy: tuple[float, float]) -> dict | None:
    """Find a reachable stop on the robot's approach side of the target."""
    if not getattr(grid, "has_free_space_evidence", False):
        return None
    try:
        start = grid.odom_to_grid(pose["x"], pose["y"])
        grid.odom_to_grid(*target_xy)
    except (KeyError, TypeError, ValueError):
        return None
    if not grid.is_free(*start):
        return None
    clearance = ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M
    dx, dy = target_xy[0] - pose["x"], target_xy[1] - pose["y"]
    initial_distance = math.hypot(dx, dy)
    if initial_distance <= APPROACH_STOP_RADIUS_M:
        if not grid.is_body_clear(pose["x"], pose["y"], pose["heading"]):
            return None
        return {"x": pose["x"], "y": pose["y"], "heading": pose["heading"],
                "distance_to_target_m": round(initial_distance, 3),
                "already_near": True}
    ux, uy = dx / initial_distance, dy / initial_distance
    ideal_x = target_xy[0] - ux * APPROACH_STOP_RADIUS_M
    ideal_y = target_xy[1] - uy * APPROACH_STOP_RADIUS_M
    blocked = grid.grid.copy()
    for iy, ix in np.argwhere(~blocked):
        wx, wy = grid.grid_to_odom(int(ix), int(iy))
        if math.hypot(wx - target_xy[0], wy - target_xy[1]) < clearance:
            blocked[iy, ix] = True
    if blocked[start[1], start[0]]:
        return None
    queue = deque([start])
    visited = {start}
    best = None
    while queue:
        ix, iy = queue.popleft()
        wx, wy = grid.grid_to_odom(ix, iy)
        heading = math.atan2(target_xy[1] - wy, target_xy[0] - wx)
        distance = math.hypot(wx - target_xy[0], wy - target_xy[1])
        approach_side = (wx - target_xy[0]) * ux + (wy - target_xy[1]) * uy <= 0
        score = (math.hypot(wx - ideal_x, wy - ideal_y),
                 math.hypot(wx - pose["x"], wy - pose["y"]))
        if (approach_side and (best is None or score < best[0])
                and grid.is_body_clear(wx, wy, heading)):
            best = (score, {"x": wx, "y": wy, "heading": heading,
                            "distance_to_target_m": round(distance, 3),
                            "already_near": False})
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                       (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = ix + dx, iy + dy
            if not (0 <= nx < blocked.shape[1] and 0 <= ny < blocked.shape[0]) or blocked[ny, nx]:
                continue
            if dx and dy and (blocked[iy, nx] or blocked[ny, ix]):
                continue
            if (nx, ny) not in visited:
                visited.add((nx, ny))
                queue.append((nx, ny))
    if best is None:
        return None
    stop = best[1]
    if math.hypot(stop["x"] - pose["x"], stop["y"] - pose["y"]) < grid.resolution:
        return None
    return stop


def save_paired_sample(image_path: Path | None, frame: dict, voxel_msg: dict | None,
                       voxel_received_at: float | None,
                       bbox_1000: list | None = None) -> Path | None:
    """Save one private JPEG-linked point sample for camera/LiDAR alignment review."""
    if image_path is None or not isinstance(voxel_msg, dict):
        emit_agent_event("vision.lidar", phase="capture", saved=False,
                         frame_id=frame.get("frame_id"), reason="voxel_unavailable")
        return None
    data = voxel_msg.get("data") or {}
    header = data.get("header") or {}
    voxel_frame_id = header.get("frame_id") or data.get("frame_id")
    pose = frame.get("pose_at_capture") or {}
    raw_points = (data.get("data") or {}).get("points")
    try:
        points = np.asarray(raw_points, dtype=np.float32)
        if points.ndim != 2 or points.shape[1] < 3:
            raise ValueError("point_shape_invalid")
        if voxel_frame_id != "odom" or pose.get("session_id") != frame.get("odom_session_id"):
            raise ValueError("frame_mismatch")
        if pose.get("z") is None or "orientation" not in pose:
            raise ValueError("pose_3d_unavailable")
        skew = (voxel_received_at - frame["received_at"]
                if isinstance(voxel_received_at, (int, float)) else None)
        if skew is None or not math.isfinite(skew):
            raise ValueError("voxel_timestamp_unavailable")
        center = np.asarray([pose["x"], pose["y"]], dtype=float)
        nearby = np.isfinite(points[:, :3]).all(axis=1)
        nearby &= np.linalg.norm(points[:, :2] - center, axis=1) <= 4.0
        points = points[nearby, :3]
        metadata = {
            "frame_id": frame.get("frame_id"), "width": frame.get("width"),
            "height": frame.get("height"), "image_received_at": frame.get("received_at"),
            "pose_received_at": frame.get("pose_received_at"),
            "voxel_received_at": voxel_received_at, "voxel_skew_s": skew,
            "odom_session_id": frame.get("odom_session_id"), "pose_at_capture": pose,
            "voxel_frame_id": voxel_frame_id,
            "voxel_resolution_m": (float(data["resolution"])
                                   if data.get("resolution") is not None else None),
            "voxel_origin": [float(v) for v in data["origin"]] if data.get("origin") else None,
            "voxel_width": [int(v) for v in data["width"]] if data.get("width") else None,
            "bbox_1000": bbox_1000,
        }
        path = image_path.with_suffix(".npz")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as file:
                np.savez_compressed(file, points_odom=points,
                                    metadata=json.dumps(metadata, ensure_ascii=False))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        emit_agent_event("vision.lidar", phase="capture", saved=True,
                         frame_id=frame.get("frame_id"), artifact=str(path),
                         point_count=len(points), voxel_skew_s=round(skew, 3),
                         pose_skew_s=round(frame["received_at"] - frame["pose_received_at"], 3)
                         if isinstance(frame.get("pose_received_at"), (int, float)) else None)
        if bbox_1000:
            try:
                pixels, depths, world = project_odom_points(
                    points, pose, nominal_camera(frame["width"], frame["height"]),
                    frame["width"], frame["height"])
                matched = bbox_mask(pixels, bbox_1000, frame["width"], frame["height"])
                floor_z = OccupancyGrid(voxel_msg, robot_pose=pose).measured_floor_z
                object_mask = object_returns_in_box(world, matched, floor_z)
                object_points = world[object_mask]
                candidate = (np.median(object_points[:, :2], axis=0)
                             if len(object_points) else None)
                emit_agent_event("vision.lidar", phase="nominal_projection",
                                 frame_id=frame.get("frame_id"),
                                 camera_model="unverified_URDF_hfov_90deg",
                                 points_in_image=len(pixels),
                                 points_in_target_box=int(np.count_nonzero(matched)),
                                 measured_floor_z_m=floor_z,
                                 object_returns_in_box=len(object_points),
                                 depth_median_m=round(float(np.median(depths[object_mask])), 3)
                                 if len(object_points) else None,
                                 candidate_odom_xy=[round(float(v), 3) for v in candidate]
                                 if candidate is not None else None)
            except (ValueError, TypeError, KeyError) as exc:
                emit_agent_event("vision.lidar", phase="nominal_projection",
                                 frame_id=frame.get("frame_id"), error_type=type(exc).__name__)
        return path
    except FileExistsError:
        return image_path.with_suffix(".npz")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        emit_agent_event("vision.lidar", phase="capture", saved=False,
                         frame_id=frame.get("frame_id"), reason=str(exc)[:80])
        return None
