"""One server-owned forward contact allowance, using fresh measured cargo.

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


class _ContactGrid:
    def __init__(self, grid, contact):
        self._grid, self._contact = grid, contact

    def __getattr__(self, name):
        return getattr(self._grid, name)

    def is_body_clear(self, x, y, heading):
        return self._contact.in_scope(x, y, heading) and self._grid.is_body_clear(x, y, heading)


class CargoContact:
    def __init__(self, sample, start, advance, context):
        self.context, self.advance = context, advance
        self.start, self.session = dict(start), sample['odom_session_id']
        self.claimed = False
        source = sample.get('contact_observation') or {}
        self.points = np.asarray(source.get('points_odom', []), dtype=float)
        if (self.points.ndim != 2 or self.points.shape[1] != 3
                or not 6 <= len(self.points) <= 512 or not np.isfinite(self.points).all()
                or type(advance) not in (int, float) or not math.isfinite(advance)
                or advance <= 0 or start.get('session_id') != self.session):
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
        shifted = self.points + [self.c * (advance + .08), self.s * (advance + .08), 0.]
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

    def in_scope(self, x, y, heading):
        dx, dy = x-self.start['x'], y-self.start['y']
        along, cross = dx*self.c + dy*self.s, -dx*self.s + dy*self.c
        turn = abs(math.atan2(math.sin(heading-self.start['heading']), math.cos(heading-self.start['heading'])))
        return (-.04 <= along <= self.advance + .05 and abs(cross) <= .05 and turn <= .15)

    def grid_for(self, grid, pose):
        """Re-associate one compact cargo; all other raw returns stay blocked."""
        if (self.context.expires_at and time.time() >= self.context.expires_at
                or pose.get('session_id') != self.session
                or not self.in_scope(pose['x'], pose['y'], pose['heading'])):
            raise TargetUnlocalized('cargo_contact_scope_changed')
        voxel = getattr(grid, 'voxel_message', None)
        if voxel is None or grid.measured_floor_z is None:
            raise TargetUnlocalized('cargo_contact_geometry_unavailable')
        measured = []
        target = associate_lidar_target(voxel, self.capture_pose, self.camera, self.frame,
            self.box, grid.measured_floor_z, target_points=measured)
        if target is None or len(measured) < 6:
            raise TargetUnlocalized('cargo_contact_tracking_lost')
        points = np.asarray(measured)
        center = np.median(points[:, :2], axis=0)
        delta = center - self.center
        progress = ((pose['x']-self.start['x'])*self.c + (pose['y']-self.start['y'])*self.s)
        along, cross = delta[0]*self.c + delta[1]*self.s, -delta[0]*self.s + delta[1]*self.c
        if (not -.06 <= along <= max(0., progress) + .10 or abs(cross) > .08
                or max(abs(np.ptp(points, axis=0)-self.extent)) > grid.resolution + 1e-6
                or not len(self.points)/1.5 <= len(points) <= len(self.points)*1.5):
            raise TargetUnlocalized('cargo_contact_tracking_lost')
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
        if len(obstacles):
            relative = obstacles[:, :2] - center
            ox, oy = relative[:, 0]*self.c + relative[:, 1]*self.s, -relative[:, 0]*self.s + relative[:, 1]*self.c
            shape = points[:, :2]-center
            sx, sy = shape[:, 0]*self.c + shape[:, 1]*self.s, -shape[:, 0]*self.s + shape[:, 1]*self.c
            margin = SAFETY_MARGIN_M
            if np.any((ox >= sx.min()-margin) & (ox <= sx.max()+self.advance-max(0., progress)+margin)
                      & (oy >= sy.min()-margin) & (oy <= sy.max()+margin)):
                raise TargetUnlocalized('cargo_contact_path_blocked')
        data = {**voxel['data'], 'data': {**voxel['data']['data'], 'points': remaining}}
        private = OccupancyGrid({**voxel, 'data': data}, robot_pose=pose)
        return _ContactGrid(private, self)
