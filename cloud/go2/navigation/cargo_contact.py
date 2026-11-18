"""One target-aware map view shared by cargo approach and forward pushing.

Ordinary navigation keeps the full obstacle map. This bounded movement uses a
private map view excluding only the uniquely re-associated cargo component.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time

import numpy as np

from cloud.go2.navigation.occupancy_grid import OccupancyGrid, SAFETY_MARGIN_M
from cloud.go2.perception.rgb_lidar import (
    associate_lidar_target, go2_camera_profile, project_odom_points, TARGET_FLOOR_BAND_M,
)
from cloud.go2.perception.visual_target import TargetUnlocalized

_pending = ContextVar('measured_cargo_contact', default=None)


@contextmanager
def permit_contact(contact):
    token = _pending.set(contact)
    try:
        yield
    finally:
        _pending.reset(token)


def claim_contact(direction, distance_m, speed, context):
    """Consume this allowance once; models cannot supply it as a parameter."""
    contact = _pending.get()
    _pending.set(None)
    if contact is not None:
        if (contact.claimed or context != contact.context or direction != 'forward'
                or distance_m != contact.advance or not 0 < speed <= .25):
            raise ValueError('cargo_contact_scope_mismatch')
        contact.claimed = True
    return contact


class CargoContact:
    def __init__(self, sample, start, advance, context):
        self.context, self.advance = context, advance
        self.start, self.session = dict(start), sample['odom_session_id']
        self.claimed = False
        source = sample.get('contact_observation') or {}
        self.points = np.asarray(source.get('points_odom', []), dtype=float)
        if (self.points.ndim != 2 or self.points.shape[1] != 3
                or not 0 < len(self.points) <= 512 or not np.isfinite(self.points).all()
                or advance is not None and (type(advance) not in (int, float)
                    or not math.isfinite(advance) or advance <= 0)
                or start.get('session_id') != self.session):
            raise TargetUnlocalized('cargo_contact_geometry_unavailable')
        self.frame, self.capture_pose = source['frame'], source['pose']
        if self.capture_pose.get('session_id') != self.session:
            raise TargetUnlocalized('cargo_contact_scope_changed')
        self.camera = go2_camera_profile(self.frame['width'], self.frame['height'])
        self.center = np.median(self.points[:, :2], axis=0)
        self.extent = np.ptp(self.points, axis=0)
        self.c, self.s = math.cos(start['heading']), math.sin(start['heading'])
        # Project the measured component's bounded forward sweep into the
        # original image as a tracking gate; no new visual facts are invented.
        shifted = self.points + [self.c * (advance or 0.), self.s * (advance or 0.), 0.]
        pixels, _, _ = project_odom_points(np.vstack([self.points, shifted]),
            self.capture_pose, self.camera, self.frame['width'], self.frame['height'])
        if not len(pixels):
            raise TargetUnlocalized('cargo_contact_geometry_unavailable')
        lo, hi = pixels.min(axis=0), pixels.max(axis=0)
        box = sample['cargo_bbox_1000']
        self.box = [max(0., min(box[0], (lo[0]-8) * 1000 / self.frame['width'])),
                    max(0., min(box[1], (lo[1]-8) * 1000 / self.frame['height'])),
                    min(1000., max(box[2], (hi[0]+8) * 1000 / self.frame['width'])),
                    min(1000., max(box[3], (hi[1]+8) * 1000 / self.frame['height']))]

    def measure(self, grid, pose):
        """Follow the confirmed component without another model decision."""
        if (self.context.expires_at and time.time() >= self.context.expires_at
                or pose.get('session_id') != self.session):
            raise TargetUnlocalized('cargo_contact_scope_changed')
        voxel = getattr(grid, 'voxel_message', None)
        if voxel is None or grid.measured_floor_z is None:
            raise TargetUnlocalized('cargo_contact_geometry_unavailable')
        measured = []
        target = associate_lidar_target(voxel, self.capture_pose, self.camera, self.frame,
            self.box, grid.measured_floor_z, target_points=measured)
        if target is None or not measured:
            raise TargetUnlocalized('cargo_contact_tracking_lost')
        points = np.asarray(measured)
        center = np.median(points[:, :2], axis=0)
        travel = math.hypot(pose['x']-self.start['x'], pose['y']-self.start['y'])
        # Identity follows the associated object, without imposing a second
        # body corridor, exact shape or point-count ratio on ordinary gait.
        if np.linalg.norm(center-self.center) > travel + max(grid.resolution, max(self.extent[:2])/2):
            raise TargetUnlocalized('cargo_contact_tracking_lost')
        return target, points

    def grid_for(self, grid, pose):
        """Exclude the same measured cargo from the complete obstacle map."""
        _target, points = self.measure(grid, pose)
        voxel = grid.voxel_message
        center = np.median(points[:, :2], axis=0)
        progress = ((pose['x']-self.start['x'])*self.c + (pose['y']-self.start['y'])*self.s)
        # Exclude exact measured 3D voxels, not a disk or rectangle that could
        # also hide a nearby person, wall or second object.
        raw = np.asarray(voxel['data']['data']['points'])
        origin = np.asarray(voxel['data']['origin'][:3])
        keys = np.rint((points-origin) / grid.resolution).astype(int)
        cargo_keys = {tuple(key) for key in keys}
        raw_keys = np.rint((raw[:, :3]-origin) / grid.resolution).astype(int)
        remaining = raw[[tuple(key) not in cargo_keys for key in raw_keys]]
        # Also protect the cargo's forward sweep, beyond the robot's body.
        obstacles = remaining[remaining[:, 2] > grid.measured_floor_z + TARGET_FLOOR_BAND_M]
        if self.advance is not None and len(obstacles):
            relative = obstacles[:, :2] - center
            ox, oy = relative[:, 0]*self.c + relative[:, 1]*self.s, -relative[:, 0]*self.s + relative[:, 1]*self.c
            shape = points[:, :2]-center
            sx, sy = shape[:, 0]*self.c + shape[:, 1]*self.s, -shape[:, 0]*self.s + shape[:, 1]*self.c
            margin = SAFETY_MARGIN_M
            if np.any((ox >= sx.min()-margin) & (ox <= sx.max()+self.advance-max(0., progress)+margin)
                      & (oy >= sy.min()-margin) & (oy <= sy.max()+margin)):
                raise TargetUnlocalized('cargo_contact_path_blocked')
        data = {**voxel['data'], 'data': {**voxel['data']['data'], 'points': remaining}}
        local = OccupancyGrid({**voxel, 'data': data}, robot_pose=pose)
        obstacles = grid.obstacles.copy()
        # Change only the cargo's cells. Old obstacles outside the current
        # voxel window and other heights at the same XY remain represented.
        for point in points:
            gx, gy = np.rint((point[:2]-np.asarray(grid.origin[:2])) / grid.resolution).astype(int)
            lx, ly = np.rint((point[:2]-origin[:2]) / grid.resolution).astype(int)
            if (0 <= gx < grid.width[0] and 0 <= gy < grid.width[1]
                    and 0 <= lx < local.width[0] and 0 <= ly < local.width[1]):
                obstacles[gy, gx] = local.obstacles[ly, lx]
        return grid.with_obstacles(obstacles)
