"""Inspect a paired Go2 JPEG/voxel sample without sending motion commands.

Usage: uv run python scripts/inspect_go2_rgb_lidar.py logs/vision/<run>-<frame>.npz
The default URDF camera placement and 90-degree FOV are diagnostic guesses.
Pass --calibration with measured camera values for an alignment check.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud.go2.navigation.occupancy_grid import OccupancyGrid, FREE, OCCUPIED
from cloud.go2.perception.rgb_lidar import (
    bbox_mask, nominal_camera, object_returns_in_box, project_odom_points,
    select_reachable_stop,
)


def _save_private_png(image: Image.Image, path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as file:
        image.save(file, format="PNG")


def inspect(sample_path: Path, calibration_path: Path | None,
            hfov_deg: float) -> dict:
    image_path = sample_path.with_suffix(".jpg")
    if not image_path.is_file():
        raise FileNotFoundError(f"paired JPEG missing: {image_path}")
    with np.load(sample_path, allow_pickle=False) as sample:
        metadata = json.loads(str(sample["metadata"]))
        points = sample["points_odom"]
    with Image.open(image_path) as image:
        canvas = image.convert("RGB")
    width, height = canvas.size
    if (width, height) != (metadata["width"], metadata["height"]):
        raise ValueError("JPEG and paired metadata dimensions differ")
    camera = (json.loads(calibration_path.read_text()) if calibration_path
              else nominal_camera(width, height, hfov_deg))
    pixels, depths, world = project_odom_points(
        points, metadata["pose_at_capture"], camera, width, height)
    draw = ImageDraw.Draw(canvas)
    # Sampling the overlay changes only the picture, never the depth statistics.
    stride = max(1, math.ceil(len(pixels) / 15000))
    for (u, v), depth in zip(pixels[::stride], depths[::stride]):
        color = (255, 70, 70) if depth < 1.0 else (255, 180, 50) if depth < 2.0 else (80, 180, 255)
        draw.point((round(float(u)), round(float(v))), fill=color)
    box = metadata.get("bbox_1000")
    matched = np.zeros(len(pixels), dtype=bool)
    if box:
        matched = bbox_mask(pixels, box, width, height)
        draw.rectangle((box[0] * width / 1000, box[1] * height / 1000,
                        box[2] * width / 1000, box[3] * height / 1000),
                       outline=(255, 230, 0), width=2)
    output = sample_path.with_suffix(".overlay.png")
    summary = {
        "overlay": str(output), "camera_source": (str(calibration_path) if calibration_path
                                                   else f"unverified_URDF_nominal_hfov_{hfov_deg:g}deg"),
        "voxel_skew_s": metadata["voxel_skew_s"],
        "pose_skew_s": (metadata["image_received_at"] - metadata["pose_received_at"]
                        if metadata.get("pose_received_at") is not None else None),
        "points_near_robot": len(points), "points_in_image": len(pixels),
        "points_in_target_box": int(np.count_nonzero(matched)),
    }
    grid = None
    if (metadata.get("voxel_origin") and metadata.get("voxel_width")
            and metadata.get("voxel_resolution_m")):
        voxel = {"data": {"frame_id": "odom",
                          "origin": metadata["voxel_origin"],
                          "width": metadata["voxel_width"],
                          "resolution": metadata["voxel_resolution_m"],
                          "data": {"points": points}}}
        grid = OccupancyGrid(voxel, robot_pose=metadata["pose_at_capture"])
    object_mask = object_returns_in_box(
        world, matched, grid.measured_floor_z if grid is not None else None)
    summary["measured_floor_z_m"] = grid.measured_floor_z if grid is not None else None
    summary["object_returns_in_target_box"] = int(np.count_nonzero(object_mask))
    for u, v in pixels[object_mask]:
        draw.ellipse((float(u) - 1, float(v) - 1, float(u) + 1, float(v) + 1),
                     fill=(40, 255, 70))
    _save_private_png(canvas, output)
    if np.any(matched):
        summary["target_box_depth_m"] = {
            "nearest": round(float(np.min(depths[matched])), 3),
            "median": round(float(np.median(depths[matched])), 3),
            "farthest": round(float(np.max(depths[matched])), 3),
        }
    if np.any(object_mask):
        summary["object_depth_median_m"] = round(float(np.median(depths[object_mask])), 3)
        candidate = np.median(world[object_mask, :2], axis=0)
        summary["unverified_object_point_odom_xy"] = [round(float(v), 3) for v in candidate]
        if grid is not None:
            try:
                stop = select_reachable_stop(grid, metadata["pose_at_capture"],
                                             (float(candidate[0]), float(candidate[1])))
                summary["unverified_reachable_stop"] = stop
                colors = np.empty((*grid.cells.shape, 3), dtype=np.uint8)
                colors[:] = (14, 18, 28)
                colors[grid.cells == FREE] = (34, 65, 70)
                colors[grid.cells == OCCUPIED] = (100, 100, 100)
                colors[grid.obstacles] = (210, 85, 75)
                map_image = Image.fromarray(colors[::-1], "RGB").resize(
                    (grid.width[0] * 3, grid.width[1] * 3), Image.Resampling.NEAREST)
                map_draw = ImageDraw.Draw(map_image)

                def dot(x: float, y: float, color: tuple[int, int, int]) -> None:
                    gx, gy = grid.odom_to_grid(x, y)
                    px, py = gx * 3 + 1, (grid.width[1] - 1 - gy) * 3 + 1
                    map_draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill=color)

                dot(metadata["pose_at_capture"]["x"],
                    metadata["pose_at_capture"]["y"], (255, 255, 255))
                dot(float(candidate[0]), float(candidate[1]), (255, 230, 0))
                if stop is not None:
                    dot(stop["x"], stop["y"], (40, 220, 255))
                map_path = sample_path.with_suffix(".map.png")
                _save_private_png(map_image, map_path)
                summary["map_preview"] = str(map_path)
            except (ValueError, TypeError, KeyError) as exc:
                summary["map_preview_error"] = type(exc).__name__
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path, help="paired .npz from a visual approach run")
    parser.add_argument("--calibration", type=Path, help="measured camera calibration JSON")
    parser.add_argument("--hfov-deg", type=float, default=90,
                        help="diagnostic nominal horizontal FOV when no calibration is supplied")
    args = parser.parse_args()
    print(json.dumps(inspect(args.sample, args.calibration, args.hfov_deg),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
