"""Local 2D grid from measured floor and obstacle voxels.

Empty voxels alone are not free-space evidence. A floor return must be present
before a cell can be used for navigation.
"""
import math

import numpy as np

Z_FLOOR_M = 0.15
Z_CEIL_M = 1.50
SELF_RETURN_RADIUS_M = 0.25
ROBOT_HALF_LENGTH_M = 0.35
ROBOT_HALF_WIDTH_M = 0.155
SAFETY_MARGIN_M = 0.07
PATH_CLEARANCE_M = ROBOT_HALF_WIDTH_M + SAFETY_MARGIN_M
TURN_CLEARANCE_M = math.hypot(ROBOT_HALF_LENGTH_M, ROBOT_HALF_WIDTH_M) + SAFETY_MARGIN_M
LOCAL_FLOOR_BRIDGE_M = SELF_RETURN_RADIUS_M + 0.25
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
        self.has_free_space_evidence = False
        self.measured_floor_z: float | None = None
        self.obstacles = None
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
                pts = pts[np.isfinite(pts[:, :3]).all(axis=1)]
                floor_z = self._measured_floor_height(pts, robot_pose)
                self.measured_floor_z = floor_z
                # The native decoder emits voxel lattice coordinates. Round
                # back to that lattice; float error at a cell edge can split
                # an otherwise continuous measured floor into blocked gaps.
                ix = np.rint((pts[:, 0] - self.origin[0]) / self.resolution).astype(int)
                iy = np.rint((pts[:, 1] - self.origin[1]) / self.resolution).astype(int)
                inside = (ix >= 0) & (iy >= 0) & (ix < nx) & (iy < ny)
                if floor_z is not None:
                    floor_hits = inside & (np.abs(pts[:, 2] - floor_z) <= 0.08)
                    cells[iy[floor_hits], ix[floor_hits]] = FREE
                    self._bridge_nearby_floor(cells, pts[floor_hits], robot_pose)
                    obstacle_hits = inside & (pts[:, 2] > floor_z + Z_FLOOR_M)
                    obstacle_hits &= pts[:, 2] < floor_z + Z_CEIL_M
                else:
                    # Without a measured floor the map cannot authorize travel,
                    # but still show obstacle returns in the diagnostic image.
                    obstacle_hits = inside & (pts[:, 2] > Z_FLOOR_M)
                    obstacle_hits &= pts[:, 2] < Z_CEIL_M
                obstacles[iy[obstacle_hits], ix[obstacle_hits]] = True
        footprint = np.zeros_like(obstacles)
        if robot_pose and all(k in robot_pose for k in ("x", "y")):
            center = self.odom_to_grid(robot_pose["x"], robot_pose["y"])
            radius = math.ceil(SELF_RETURN_RADIUS_M / self.resolution)
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if (dx * self.resolution) ** 2 + (dy * self.resolution) ** 2 > SELF_RETURN_RADIUS_M ** 2:
                        continue
                    ix, iy = center[0] + dx, center[1] + dy
                    if 0 <= ix < nx and 0 <= iy < ny:
                        footprint[iy, ix] = True
            # Points within the measured body footprint are robot returns.
            obstacles[footprint] = False
        self.obstacles = obstacles
        radius = math.ceil(PATH_CLEARANCE_M / self.resolution)
        inflated = obstacles.copy()
        ys, xs = np.where(obstacles)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if (dx * self.resolution) ** 2 + (dy * self.resolution) ** 2 > PATH_CLEARANCE_M ** 2:
                    continue
                ix, iy = xs + dx, ys + dy
                valid = (ix >= 0) & (iy >= 0) & (ix < nx) & (iy < ny)
                inflated[iy[valid], ix[valid]] = True
        cells[footprint] = FREE
        cells[inflated] = OCCUPIED
        self.has_free_space_evidence = bool(np.any((cells == FREE) & ~footprint))
        return cells

    def _bridge_nearby_floor(self, cells: np.ndarray, floor_points: np.ndarray,
                             robot_pose: dict | None) -> None:
        """Fill only the short lidar blind gap to nearby measured floor hits.

        Each opened cell lies on a ray ending at an observed floor cell on the
        measured plane. The obstacle inflation below still takes precedence.
        """
        if not robot_pose or not len(floor_points):
            return
        x, y = robot_pose["x"], robot_pose["y"]
        dx = floor_points[:, 0] - x
        dy = floor_points[:, 1] - y
        distance = np.hypot(dx, dy)
        near = (distance > SELF_RETURN_RADIUS_M
                ) & (distance <= LOCAL_FLOOR_BRIDGE_M)
        if not np.any(near):
            return
        # One measured endpoint per five-degree sector is enough to join the
        # footprint to the floor patch without inventing an open disk.
        angles = np.mod(np.arctan2(dy[near], dx[near]), 2 * math.pi)
        sectors = np.floor(angles * 72 / (2 * math.pi)).astype(int)
        endpoints = floor_points[near]
        lengths = distance[near]
        for sector in np.unique(sectors):
            members = np.flatnonzero(sectors == sector)
            endpoint = endpoints[members[np.argmax(lengths[members])]]
            steps = max(1, math.ceil(math.hypot(endpoint[0] - x, endpoint[1] - y)
                                      / self.resolution))
            for step in range(1, steps + 1):
                ratio = step / steps
                wx = x + (endpoint[0] - x) * ratio
                wy = y + (endpoint[1] - y) * ratio
                try:
                    ix, iy = self.odom_to_grid(wx, wy)
                except ValueError:
                    break
                if cells[iy, ix] == UNKNOWN:
                    cells[iy, ix] = FREE

    def _measured_floor_height(self, pts: np.ndarray, robot_pose: dict | None) -> float | None:
        """Accept only a broad, locally measured horizontal floor patch."""
        if not robot_pose or not all(k in robot_pose for k in ("x", "y")) or len(pts) < 30:
            return None
        x, y = float(robot_pose["x"]), float(robot_pose["y"])
        nearby = pts[((pts[:, 0] - x) ** 2 + (pts[:, 1] - y) ** 2 <= 1.5 ** 2)
                     & (pts[:, 2] <= 0.15)]
        if len(nearby) < 30:
            return None
        bins = np.floor(nearby[:, 2] / 0.1).astype(int)
        values, counts = np.unique(bins, return_counts=True)
        floor_z = (values[int(np.argmax(counts))] + 0.5) * 0.1
        patch = nearby[np.abs(nearby[:, 2] - floor_z) <= 0.08]
        if (len(patch) < 30 or np.ptp(patch[:, 0]) < 0.5
                or np.ptp(patch[:, 1]) < 0.5):
            return None
        return floor_z

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

    def is_body_clear(self, x: float, y: float, heading: float) -> bool:
        """Check the oriented body against measured obstacles before motion."""
        try:
            ix, iy = self.odom_to_grid(x, y)
        except ValueError:
            return False
        if not self.is_free(ix, iy) or self.obstacles is None:
            return False
        c, s = math.cos(heading), math.sin(heading)
        for forward in (-ROBOT_HALF_LENGTH_M - SAFETY_MARGIN_M,
                        ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M):
            for lateral in (-PATH_CLEARANCE_M, PATH_CLEARANCE_M):
                try:
                    self.odom_to_grid(x + forward * c - lateral * s,
                                      y + forward * s + lateral * c)
                except ValueError:
                    return False
        radius = math.ceil(TURN_CLEARANCE_M / self.resolution)
        left, right = max(0, ix - radius), min(self.width[0], ix + radius + 1)
        bottom, top = max(0, iy - radius), min(self.width[1], iy + radius + 1)
        ys, xs = np.nonzero(self.obstacles[bottom:top, left:right])
        if not len(xs):
            return True
        dx = self.origin[0] + (xs + left + 0.5) * self.resolution - x
        dy = self.origin[1] + (ys + bottom + 0.5) * self.resolution - y
        along = dx * c + dy * s
        across = -dx * s + dy * c
        return not bool(np.any((np.abs(along) <= ROBOT_HALF_LENGTH_M + SAFETY_MARGIN_M)
                               & (np.abs(across) <= PATH_CLEARANCE_M)))

    def is_turn_clear(self, x: float, y: float) -> bool:
        """Turning in place needs the full body diagonal clear of obstacles."""
        try:
            ix, iy = self.odom_to_grid(x, y)
        except ValueError:
            return False
        if not self.is_free(ix, iy) or self.obstacles is None:
            return False
        if (x - TURN_CLEARANCE_M < self.origin[0]
                or y - TURN_CLEARANCE_M < self.origin[1]
                or x + TURN_CLEARANCE_M >= self.origin[0] + self.width[0] * self.resolution
                or y + TURN_CLEARANCE_M >= self.origin[1] + self.width[1] * self.resolution):
            return False
        radius = math.ceil(TURN_CLEARANCE_M / self.resolution)
        left, right = max(0, ix - radius), min(self.width[0], ix + radius + 1)
        bottom, top = max(0, iy - radius), min(self.width[1], iy + radius + 1)
        ys, xs = np.nonzero(self.obstacles[bottom:top, left:right])
        dx = self.origin[0] + (xs + left + 0.5) * self.resolution - x
        dy = self.origin[1] + (ys + bottom + 0.5) * self.resolution - y
        return not bool(np.any(dx * dx + dy * dy <= TURN_CLEARANCE_M ** 2))
