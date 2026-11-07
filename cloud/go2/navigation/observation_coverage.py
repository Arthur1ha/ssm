"""Task-local camera coverage, separate from geometric free-space evidence."""
from collections import OrderedDict, deque
import math

import numpy as np

from cloud.go2.navigation.occupancy_grid import FREE

CAMERA_FOV_RAD = math.radians(70)
MIN_VIEW_M = .35
MAX_VIEW_M = 3.0
MIN_IMAGE_QUALITY = .6
MAX_COVERAGE_CELLS = 50_000


def visible_cells(grid, pose, *, bounds=None):
    """Conservative ground rays; obstacles and unknown cells occlude farther cells."""
    resolution = grid.resolution
    rays = max(3, math.ceil(CAMERA_FOV_RAD * MAX_VIEW_M / resolution))
    angles = np.linspace(pose['heading'] - CAMERA_FOV_RAD / 2,
                         pose['heading'] + CAMERA_FOV_RAD / 2, rays + 1)
    distances = np.arange(1, math.ceil(MAX_VIEW_M / resolution) + 1) * resolution
    x = pose['x'] + np.cos(angles[:, None]) * distances
    y = pose['y'] + np.sin(angles[:, None]) * distances
    ix = np.floor((x - grid.origin[0]) / resolution).astype(int)
    iy = np.floor((y - grid.origin[1]) / resolution).astype(int)
    valid = (ix >= 0) & (iy >= 0) & (ix < grid.width[0]) & (iy < grid.width[1])
    if bounds is not None:
        valid &= (x >= bounds[0]) & (x <= bounds[2]) & (y >= bounds[1]) & (y <= bounds[3])
    free = np.zeros_like(valid)
    free[valid] = grid.cells[iy[valid], ix[valid]] == FREE
    visible = np.logical_and.accumulate(free, axis=1) & (distances >= MIN_VIEW_M)
    # Unique map cells before making stable world-lattice keys. Vectorization
    # keeps full-map candidate scoring off the Python receiver/control GIL.
    cells = np.unique(iy[visible] * grid.width[0] + ix[visible])
    wx = grid.origin[0] / resolution + cells % grid.width[0] + .5
    wy = grid.origin[1] / resolution + cells // grid.width[0] + .5
    return set(zip(np.floor(wx + 1e-6).astype(int), np.floor(wy + 1e-6).astype(int)))



class ObservationCoverage:
    def __init__(self, map_session: str, *, bounds=None, max_records=256):
        self.map_session = map_session
        self.bounds = bounds
        self.records = deque(maxlen=max_records)
        self._seen = OrderedDict()
        self._last_image_time = float('-inf')
        self._failures = {}

    def record(self, grid, pose, image_time, quality, target_result=None):
        if (getattr(grid, 'map_session', self.map_session) != self.map_session
                or pose.get('session_id', self.map_session) != self.map_session
                or not math.isfinite(image_time) or image_time <= self._last_image_time
                or not math.isfinite(quality) or quality < MIN_IMAGE_QUALITY):
            return False
        cells = visible_cells(grid, pose, bounds=self.bounds)
        self._last_image_time = image_time
        for cell in cells:
            self._seen[cell] = image_time
            self._seen.move_to_end(cell)
        while len(self._seen) > MAX_COVERAGE_CELLS:
            self._seen.popitem(last=False)
        self.records.append({'map_session': self.map_session,
                             'pose': {k: pose[k] for k in ('x', 'y', 'heading')},
                             'image_time': image_time, 'quality': quality,
                             'visible_cells': len(cells), 'target_result': target_result})
        return True

    def gain(self, grid, pose):
        return len(visible_cells(grid, pose, bounds=self.bounds) - self._seen.keys())

    def visits(self, pose):
        return sum(math.hypot(record['pose']['x'] - pose['x'], record['pose']['y'] - pose['y']) < .35
                   and abs((record['pose']['heading'] - pose['heading'] + math.pi) % (2 * math.pi) - math.pi) < .3
                   for record in self.records)

    def mark_failed(self, target):
        key = (round(target.x, 1), round(target.y, 1), round(target.heading, 1))
        self._failures[key] = self._failures.get(key, 0) + 1
        if len(self._failures) > 256:
            self._failures.pop(next(iter(self._failures)))

    def failures(self, target):
        return self._failures.get((round(target.x, 1), round(target.y, 1), round(target.heading, 1)), 0)

    def summary(self, grid=None):
        result = {'observations': len(self.records), 'observed_cells': len(self._seen),
                  'map_session': self.map_session,
                  'failed_observation_points': len(self._failures)}
        if grid is not None:
            free = 0
            unseen = 0
            for iy, ix in zip(*((grid.cells == FREE).nonzero())):
                x, y = grid.grid_to_odom(int(ix), int(iy))
                if self.bounds and not (self.bounds[0] <= x <= self.bounds[2] and self.bounds[1] <= y <= self.bounds[3]):
                    continue
                free += 1
                unseen += (math.floor(x / grid.resolution + 1e-6),
                           math.floor(y / grid.resolution + 1e-6)) not in self._seen
            result.update(known_free_cells=free, unchecked_known_cells=unseen,
                          unchecked_area_m2=round(unseen * grid.resolution ** 2, 3),
                          occluded_or_unknown='not_checked')
        return result
