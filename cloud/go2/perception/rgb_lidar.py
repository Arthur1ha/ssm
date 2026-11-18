"""Project measured odom LiDAR into RGB with explicitly sourced camera models.

An upstream model is an association hypothesis, not a measured target range.
Only isolated foreground LiDAR clusters can provide metric target positions.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from cloud.observability import emit_agent_event
from cloud.go2.navigation.occupancy_grid import (
    OccupancyGrid, ROBOT_HALF_LENGTH_M, SAFETY_MARGIN_M, Z_FLOOR_M,
)

APPROACH_STOP_RADIUS_M = 2 * ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M + 0.15
# Match the raw measured-floor band, not the navigation obstacle-height gate.
# A small floor object can be visible to LiDAR below the body's obstacle cutoff.
TARGET_FLOOR_BAND_M = 0.08

GO2_CAMERA_SOURCE = ('https://github.com/dimensionalOS/dimos/blob/'
    '3645017c7ace59badd938283b30439a83545fcd7/'
    'dimos/robot/unitree/go2/front_camera_720.yaml')


def go2_camera_profile(width: int, height: int) -> dict:
    """DimOS Go2 model; same-FOV scaling remains an explicit device assumption.

    This is not a per-robot calibration and never carries measured/verified
    flags. Different crops/aspect ratios must not silently reuse this model.
    """
    if (type(width) is not int or type(height) is not int or width <= 0
            or height <= 0 or width * 720 != height * 1280):
        raise ValueError('camera_profile_resolution_mismatch')
    scale = width / 1280
    return {
        'fx': 797.4756164864929 * scale, 'fy': 796.4872112769983 * scale,
        'cx': 643.5352167821186 * scale, 'cy': 349.2783605343087 * scale,
        'distortion_model': 'equidistant',
        'distortion_coefficients': [-0.07309428880537933, -0.02341140740909078,
                                    -0.0069305931780026956, 0.009238684474464793],
        'translation_body_from_camera_m': [0.3, 0., 0.],
        'rotation_body_from_camera': [[0, 0, 1], [-1, 0, 0], [0, -1, 0]],
        'profile': 'dimos_go2_front_720_scaled', 'source_url': GO2_CAMERA_SOURCE,
        'extrinsics_source_url': GO2_CAMERA_SOURCE.rsplit('/', 1)[0] + '/connection.py#L129-L143',
        'measurement_status': 'upstream_model_not_per_robot_calibration',
        'scaling_assumption': 'same_field_of_view_no_crop',
    }


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


def quaternion_matrix(values: list[float]) -> np.ndarray:
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
    body_from_odom = quaternion_matrix(pose["orientation"])
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
    if camera.get('distortion_coefficients') is not None and len(points_camera):
        camera_matrix = np.array([[intrinsics[0], 0, intrinsics[2]],
                                  [0, intrinsics[1], intrinsics[3]], [0, 0, 1]], dtype=float)
        coefficients = np.asarray(camera['distortion_coefficients'], dtype=float)
        if camera.get('distortion_model') == 'equidistant':
            if coefficients.shape != (4,) or not np.isfinite(coefficients).all():
                raise ValueError('invalid equidistant distortion')
            pixels, _ = cv2.fisheye.projectPoints(points_camera.reshape(-1, 1, 3),
                np.zeros(3), np.zeros(3), camera_matrix, coefficients)
        else:
            pixels, _ = cv2.projectPoints(points_camera, np.zeros(3), np.zeros(3), camera_matrix,
                                         coefficients)
        pixels = pixels.reshape(-1, 2)
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
            "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
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


def target_cluster(points: np.ndarray, ground_xy: tuple[float, float],
                   uncertainty_m: float) -> dict | None:
    """Associate a compact connected object cluster with ground-contact evidence.

    A background wall in the bbox cannot move the estimate to its median.
    Ambiguous multiple clusters close to the contact are rejected.
    """
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] < 3 or len(points) < 3:
        return None
    points = points[np.isfinite(points[:, :3]).all(axis=1), :3]
    points = points[np.linalg.norm(points[:, :2] - ground_xy, axis=1) <= 0.30 + uncertainty_m]
    if not 3 <= len(points) <= 4096:
        return None
    # Bounded voxel adjacency avoids quadratic all-pairs clustering.
    voxels = {}
    for point in points:
        key = tuple(np.floor(point / 0.10).astype(int))
        voxels.setdefault(key, []).append(point)
    remaining = set(voxels)
    clusters = []
    while remaining:
        root = remaining.pop()
        queue = deque([root])
        cluster = []
        while queue:
            key = queue.popleft()
            cluster.extend(voxels[key])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        neighbor = (key[0] + dx, key[1] + dy, key[2] + dz)
                        if neighbor in remaining:
                            remaining.remove(neighbor)
                            queue.append(neighbor)
        array = np.asarray(cluster)
        if len(array) >= 3 and np.max(np.ptp(array[:, :2], axis=0)) <= 0.60:
            center = np.median(array[:, :2], axis=0)
            if np.linalg.norm(center - ground_xy) <= 0.20 + uncertainty_m:
                clusters.append({'xy': center.tolist(), 'points': len(array)})
    return clusters[0] if len(clusters) == 1 else None


def associate_ground_target(voxel: dict, pose: dict, camera: dict, frame: dict,
                            bbox_1000: list, floor_z: float, location: dict) -> dict | None:
    """Compute bbox/ground-contact agreement without executing device actions."""
    points = np.asarray(voxel['data']['data']['points'], dtype=float)
    pixels, _, world = project_odom_points(points, pose, camera, frame['width'], frame['height'])
    matched = object_returns_in_box(
        world, bbox_mask(pixels, bbox_1000, frame['width'], frame['height']), floor_z)
    return target_cluster(world[matched], (location['x'], location['y']), location['uncertainty_m'])


def image_bearing(camera: dict, bbox: list, width: int, height: int) -> float:
    """Horizontal bbox ray in body coordinates; positive means robot left."""
    pixel = np.array([[[(bbox[0] + bbox[2]) * width / 2000,
                        (bbox[1] + bbox[3]) * height / 2000]]], dtype=float)
    matrix = np.array([[camera['fx'], 0, camera['cx']],
                       [0, camera['fy'], camera['cy']], [0, 0, 1]], dtype=float)
    if camera.get('distortion_model') == 'equidistant':
        ray = cv2.fisheye.undistortPoints(pixel, matrix,
            np.asarray(camera['distortion_coefficients'], dtype=float))[0, 0]
    else:
        ray = cv2.undistortPoints(pixel, matrix,
            np.asarray(camera.get('distortion_coefficients', [0]*4), dtype=float))[0, 0]
    body_ray = np.asarray(camera['rotation_body_from_camera']) @ np.r_[ray, 1.]
    return math.atan2(float(body_ray[1]), float(body_ray[0]))


def associate_lidar_target(voxel: dict, pose: dict, camera: dict, frame: dict,
                           bbox_1000: list, floor_z: float, *,
                           diagnostics: dict | None = None) -> dict | None:
    """Find one isolated, compact foreground component, using actual LiDAR range.

    Grow components outside the bbox too: cropping a wall to a small image box
    must not turn that wall into a small object. Padding tolerates a few pixels
    of model/box error, but never supplies a nominal-ray distance.
    """
    report = diagnostics if diagnostics is not None else {}
    report.update(reason='measured_floor_unavailable', components=[],
                  floor_z_m=floor_z, floor_band_m=TARGET_FLOOR_BAND_M)
    if floor_z is None or not math.isfinite(floor_z):
        return None
    points = np.asarray(voxel['data']['data']['points'], dtype=float)
    width, height = frame['width'], frame['height']
    pixels, depths, world = project_odom_points(points, pose, camera, width, height)
    exact = bbox_mask(pixels, bbox_1000, width, height)
    left, top, right, bottom = np.asarray(bbox_1000) * [width, height, width, height] / 1000
    padding = min(8 * width / 640, .5 * min(right - left, bottom - top))
    foreground = (world[:, 2] > floor_z + TARGET_FLOOR_BAND_M) & (depths <= 4.)
    report.update(points_in_image=len(world), points_in_exact_box=int(exact.sum()),
                  foreground_in_exact_box=int(np.count_nonzero(exact & foreground)),
                  bbox_padding_px=float(padding))
    pixels, depths, world = pixels[foreground], depths[foreground], world[foreground]
    matched = ((pixels[:, 0] >= left-padding) & (pixels[:, 0] <= right+padding)
               & (pixels[:, 1] >= top-padding) & (pixels[:, 1] <= bottom+padding))
    report.update(foreground_points=len(world), foreground_in_padded_box=int(matched.sum()))
    if len(world):
        delta = np.maximum(np.maximum([left, top]-pixels, pixels-[right, bottom]), 0)
        report['nearest_foreground_bbox_distance_px'] = float(np.min(np.linalg.norm(delta, axis=1)))
    if np.count_nonzero(matched) < 3:
        report['reason'] = 'insufficient_foreground_returns'
        return None
    if len(world) > 30_000:
        report['reason'] = 'point_budget_exceeded'
        return None
    resolution = float(voxel['data'].get('resolution', .05))
    if not math.isfinite(resolution) or not .01 <= resolution <= .10:
        report['reason'] = 'voxel_resolution_invalid'
        return None
    link = max(.06, min(.12, resolution * 1.8))
    keys = np.floor(world / link).astype(int)
    buckets = {}
    for index, key in enumerate(keys):
        buckets.setdefault(tuple(key), []).append(index)
    seen, candidates = set(), []
    for seed in np.flatnonzero(matched):
        if seed in seen:
            continue
        seen.add(seed)
        queue, indices = deque([seed]), []
        while queue:
            index = queue.popleft()
            indices.append(index)
            x, y, z = map(int, keys[index])
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for neighbor in buckets.get((x+dx, y+dy, z+dz), ()):
                            if neighbor not in seen and np.linalg.norm(world[index]-world[neighbor]) <= link:
                                seen.add(neighbor)
                                queue.append(neighbor)
        indices = np.asarray(indices)
        component = world[indices]
        support = int(np.count_nonzero(matched[indices]))
        extent = np.ptp(component, axis=0)
        distance = float(np.median(depths[indices]))
        rejected = []
        if len(indices) > 512:
            rejected.append('component_point_budget')
        if support < 3:
            rejected.append('insufficient_component_support')
        if support < len(indices) * .5:
            rejected.append('component_mostly_outside_box')
        if max(extent[:2]) > .60 or extent[2] > .80:
            rejected.append('component_not_compact')
        # Voxel centers are commonly float32. One voxel's vertical extent must
        # not fail solely because its subtraction rounds just below resolution.
        if extent[2] + 1e-6 < resolution:
            rejected.append('vertical_extent_unresolved')
        if component[:, 2].min() > floor_z + .25:
            rejected.append('component_above_ground_target_band')
        if distance > float(np.min(depths[matched])) + .25:
            rejected.append('component_behind_nearer_returns')
        if len(report['components']) < 64:
            report['components'].append({'points': len(indices), 'bbox_support': support,
                'extent_xyz_m': extent.tolist(), 'depth_m': distance, 'rejected_by': rejected})
        if rejected:
            continue
        center = np.median(component[:, :2], axis=0)
        radius = float(np.max(np.linalg.norm(component[:, :2]-center, axis=1)))
        uncertainty = .10 + resolution + radius + distance * (padding+4) / min(camera['fx'], camera['fy'])
        candidates.append({'x': float(center[0]), 'y': float(center[1]),
                           'uncertainty_m': uncertainty, 'lidar_points': support,
                           'cluster_points': len(indices), 'bbox_padding_px': padding})
    report.update(candidate_count=len(candidates), reason=(
        'associated' if len(candidates) == 1 else
        'ambiguous_components' if len(candidates) > 1 else 'no_isolated_foreground_component'))
    return candidates[0] if len(candidates) == 1 else None
