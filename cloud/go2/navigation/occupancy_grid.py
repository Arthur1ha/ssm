"""A conservative local voxel projection. Unobserved cells are never free space."""
import math

import numpy as np

Z_FLOOR_M = 0.15
Z_CEIL_M = 1.50
ROBOT_RADIUS_M = 0.25
SAFETY_MARGIN_M = 0.10
UNKNOWN = 0
FREE = 1
OCCUPIED = 2


class OccupancyGrid:
    def __init__(self, voxel_msg: dict, robot_pose: dict | None = None) -> None:
        d = voxel_msg.get("data", {})
        self.resolution = float(d.get("resolution", 0.05))
        self.origin = d.get("origin", [0.0, 0.0, 0.0])
        self.width = d.get("width", [128, 128, 38])
        if not math.isfinite(self.resolution) or self.resolution <= 0:
            raise ValueError("invalid voxel resolution")
        if len(self.origin) < 2 or len(self.width) < 2:
            raise ValueError("invalid voxel geometry")
        self.frame_id = (d.get("header") or {}).get("frame_id") or d.get("frame_id")
        # The native voxel payload lists obstacle points, not observed free rays.
        # A navigation map provider must supply real free-space evidence before
        # this projection may authorize motion.
        self.has_free_space_evidence = False
        self.cells = self._build(d.get("data", {}).get("points"), robot_pose)
        # Existing A* accepts a blocked bitmap. UNKNOWN is blocked too.
        self.grid = self.cells != FREE

    def _build(self, points, robot_pose: dict | None) -> np.ndarray:
        nx, ny = int(self.width[0]), int(self.width[1])
        if nx <= 0 or ny <= 0:
            raise ValueError("invalid voxel dimensions")
        cells = np.full((ny, nx), UNKNOWN, dtype=np.uint8)
        obstacles = np.zeros((ny, nx), dtype=bool)
        if points is not None:
            pts = np.asarray(points)
            if pts.ndim == 2 and pts.shape[1] >= 3 and len(pts):
                valid = np.isfinite(pts[:, :3]).all(axis=1)
                valid &= (pts[:, 2] > Z_FLOOR_M) & (pts[:, 2] < Z_CEIL_M)
                pts = pts[valid]
                ix = np.floor((pts[:, 0] - self.origin[0]) / self.resolution).astype(int)
                iy = np.floor((pts[:, 1] - self.origin[1]) / self.resolution).astype(int)
                valid = (ix >= 0) & (iy >= 0) & (ix < nx) & (iy < ny)
                obstacles[iy[valid], ix[valid]] = True
        footprint = np.zeros_like(obstacles)
        if robot_pose and all(k in robot_pose for k in ("x", "y")):
            center = self.odom_to_grid(robot_pose["x"], robot_pose["y"])
            radius = math.ceil(ROBOT_RADIUS_M / self.resolution)
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if (dx * self.resolution) ** 2 + (dy * self.resolution) ** 2 > ROBOT_RADIUS_M ** 2:
                        continue
                    ix, iy = center[0] + dx, center[1] + dy
                    if 0 <= ix < nx and 0 <= iy < ny:
                        footprint[iy, ix] = True
            # Points within the measured body footprint are robot returns.
            obstacles[footprint] = False
        radius = math.ceil((ROBOT_RADIUS_M + SAFETY_MARGIN_M) / self.resolution)
        inflated = obstacles.copy()
        ys, xs = np.where(obstacles)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if (dx * self.resolution) ** 2 + (dy * self.resolution) ** 2 > (ROBOT_RADIUS_M + SAFETY_MARGIN_M) ** 2:
                    continue
                ix, iy = xs + dx, ys + dy
                valid = (ix >= 0) & (iy >= 0) & (ix < nx) & (iy < ny)
                inflated[iy[valid], ix[valid]] = True
        cells[footprint] = FREE
        cells[inflated] = OCCUPIED
        return cells

    def odom_to_grid(self, x: float, y: float) -> tuple[int, int]:
        ix = math.floor((x - self.origin[0]) / self.resolution)
        iy = math.floor((y - self.origin[1]) / self.resolution)
        if not (0 <= ix < self.width[0] and 0 <= iy < self.width[1]):
            raise ValueError("coordinate outside voxel grid")
        return ix, iy

    def grid_to_odom(self, ix: int, iy: int) -> tuple[float, float]:
        if not (0 <= ix < self.width[0] and 0 <= iy < self.width[1]):
            raise ValueError("cell outside voxel grid")
        return (self.origin[0] + (ix + 0.5) * self.resolution,
                self.origin[1] + (iy + 0.5) * self.resolution)

    def is_free(self, ix: int, iy: int) -> bool:
        return 0 <= ix < self.width[0] and 0 <= iy < self.width[1] and self.cells[iy, ix] == FREE
