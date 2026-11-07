"""Bounded odom-session evidence fusion; no persistent/relocalized coordinates.

Decoded voxels are already in odom. Missing returns never clear obstacles.
The receiver submits into one active job and one replaceable pending slot.
"""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import numpy as np

from cloud.go2.navigation.occupancy_grid import (
    OccupancyGrid, FREE, UNKNOWN, OCCUPIED, PATH_CLEARANCE_M,
)

MAX_MAP_CELLS = 400_000


class SessionMap:
    def __init__(self, max_cells: int = MAX_MAP_CELLS):
        self.max_cells = max_cells
        self._lock = threading.RLock()
        self._executor = None
        self._running = False
        self._pending = None
        self._snapshot = None
        self.session_id = None
        self.last_error = None
        self.dropped_frames = 0
        self._generation = 0

    def reset(self, session_id=None):
        with self._lock:
            self._generation += 1
            self.session_id = session_id
            self._snapshot = None
            self._pending = None
            self.last_error = None

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def submit(self, voxel_msg, robot_pose, *, session_id, observed_at=None):
        with self._lock:
            if session_id != self.session_id:
                self.reset(session_id)
            job = (voxel_msg, dict(robot_pose), session_id,
                   time.monotonic() if observed_at is None else observed_at,
                   self._generation)
            if self._pending is not None:
                self.dropped_frames += 1
            self._pending = job
            if not self._running:
                self._running = True
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='go2-map')
                self._executor.submit(self._drain)

    def _drain(self):
        while True:
            with self._lock:
                job, self._pending = self._pending, None
                if job is None:
                    self._running = False
                    return
            msg, pose, session, stamp, generation = job
            try:
                self._validate_geometry(msg)
                local = OccupancyGrid(msg, robot_pose=pose)
                self._merge(local, session, stamp, generation=generation)
            except (ValueError, TypeError, IndexError, OverflowError) as exc:
                with self._lock:
                    self.last_error = str(exc)

    def update(self, voxel_msg, robot_pose, *, session_id, observed_at=None):
        self._validate_geometry(voxel_msg)
        local = OccupancyGrid(voxel_msg, robot_pose=robot_pose)
        with self._lock:
            if session_id != self.session_id:
                self.reset(session_id)
            generation = self._generation
        return self._merge(local, session_id,
                           time.monotonic() if observed_at is None else observed_at,
                           generation=generation)

    def _validate_geometry(self, message):
        data = message.get('data', {})
        width = data.get('width', [128, 128, 38])
        resolution = data.get('resolution', .05)
        if (len(width) < 2 or any(type(v) not in (int, float) or not np.isfinite(v)
                                  or v <= 0 or int(v) != v for v in width[:2])
                or int(width[0]) * int(width[1]) > self.max_cells
                or not isinstance(resolution, (float, int)) or not np.isfinite(resolution)
                or resolution <= 0):
            raise ValueError('map_geometry_or_memory_limit')

    def _merge(self, local, session_id, stamp, *, generation):
        with self._lock:
            if generation != self._generation or session_id != self.session_id:
                return None
            previous = self._snapshot
        if not session_id or local.frame_id != 'odom':
            self.last_error = 'frame_or_session_invalid'
            return None
        if previous is not None and stamp <= previous.observed_at:
            return previous
        resolution = local.resolution
        if previous is not None and not np.isclose(previous.resolution, resolution):
            self.last_error = 'map_resolution_changed'
            return None
        origin = np.asarray(local.origin[:2], dtype=float)
        end = origin + np.asarray(local.width[:2]) * resolution
        if previous is not None:
            origin = np.minimum(origin, previous.origin[:2])
            end = np.maximum(end, np.asarray(previous.origin[:2]) + np.asarray(previous.width[:2]) * resolution)
        width = np.rint((end - origin) / resolution).astype(int)
        if np.prod(width, dtype=np.int64) > self.max_cells:
            self.last_error = 'map_memory_limit'
            return None
        # Native origins must lie on the same lattice; do not silently round a shifted frame.
        for source in (local, previous):
            if source is not None:
                shift = (np.asarray(source.origin[:2]) - origin) / resolution
                if not np.allclose(shift, np.rint(shift), atol=1e-4):
                    self.last_error = 'map_lattice_changed'
                    return None
        shape = (int(width[1]), int(width[0]))
        floor = np.zeros(shape, bool)
        obstacles = np.zeros(shape, bool)
        hits = np.zeros(shape, np.uint16)
        observed = np.zeros(shape, float)
        pending = np.zeros(shape, bool)
        def region(source):
            ix, iy = np.rint((np.asarray(source.origin[:2]) - origin) / resolution).astype(int)
            return np.s_[iy:iy + source.width[1], ix:ix + source.width[0]]
        if previous is not None:
            sl = region(previous)
            floor[sl] = previous.known_floor
            obstacles[sl] = previous.obstacles
            hits[sl] = previous.floor_observations
            observed[sl] = previous.last_observed
            pending[sl] = previous.pending_recheck
        sl = region(local)
        measured = local.measured_floor
        local_floor = local.known_floor
        # A floor voxel under an old obstacle is not evidence that its body volume
        # is empty. Mark the contradiction for another view and remain blocked.
        pending[sl] |= obstacles[sl] & measured & ~local.obstacles
        floor[sl] |= local_floor
        hits[sl] = np.minimum(hits[sl].astype(np.uint32) + measured, 65535).astype(np.uint16)
        obstacles[sl] |= local.obstacles
        pending[sl][local.obstacles] = False
        observed[sl][measured | local.obstacles] = stamp
        # Fill only one-cell holes surrounded by independently repeated floor
        # evidence. Unknown edges, obstacle-adjacent gaps and large holes remain.
        reliable = (hits >= 2) & ~obstacles
        near_obstacle = obstacles.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sy = slice(max(0, -dy), min(shape[0], shape[0] - dy))
                sx = slice(max(0, -dx), min(shape[1], shape[1] - dx))
                ty = slice(max(0, dy), min(shape[0], shape[0] + dy))
                tx = slice(max(0, dx), min(shape[1], shape[1] + dx))
                near_obstacle[ty, tx] |= obstacles[sy, sx]
        fill = np.zeros(shape, bool)
        fill[1:-1, 1:-1] = (reliable[:-2, 1:-1] & reliable[2:, 1:-1]
                            & reliable[1:-1, :-2] & reliable[1:-1, 2:])
        floor |= fill & ~near_obstacle
        inflated = obstacles.copy()
        radius = int(np.ceil(PATH_CLEARANCE_M / resolution))
        ys, xs = np.nonzero(obstacles)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if (dx * resolution) ** 2 + (dy * resolution) ** 2 > PATH_CLEARANCE_M ** 2:
                    continue
                xx, yy = xs + dx, ys + dy
                valid = (xx >= 0) & (xx < shape[1]) & (yy >= 0) & (yy < shape[0])
                inflated[yy[valid], xx[valid]] = True
        result = object.__new__(OccupancyGrid)
        result.resolution = resolution
        result.origin = tuple(origin) + (0.0,)
        result.width = (int(width[0]), int(width[1]), 1)
        result.frame_id = 'odom'
        result.map_session = result.session_id = session_id
        result.version = 1 if previous is None else previous.version + 1
        result.observed_at = stamp
        result.known_floor = floor
        result.measured_floor = hits > 0
        result.floor_observations = hits
        result.obstacles = obstacles
        result.pending_recheck = pending
        result.last_observed = observed
        result.measured_floor_z = local.measured_floor_z
        result.cells = np.where(inflated, OCCUPIED, np.where(floor, FREE, UNKNOWN)).astype(np.uint8)
        result.grid = result.cells != FREE
        result.has_free_space_evidence = bool(np.any(hits))
        for value in vars(result).values():
            if isinstance(value, np.ndarray):
                value.flags.writeable = False
        with self._lock:
            if generation != self._generation or self._snapshot is not previous:
                return None
            self.last_error = None
            self._snapshot = result
        return result
