"""Resample manual velocity targets without tying hardware timing to HTTP RTT."""

import asyncio
import logging

from cloud.go2.connection.fsm import CommandBlocked

logger = logging.getLogger(__name__)

_INTERVAL_S = 1 / 30
_ACCELERATION = (.6, .6, 1.2)  # m/s², m/s², rad/s²; zero input always stops immediately.


def slew_velocity(current, target, dt):
    result = []
    for value, goal, acceleration in zip(current, target, _ACCELERATION):
        if goal == 0:
            result.append(0.0)
            continue
        # Decelerate through zero before reversing direction.
        desired = 0.0 if value * goal < 0 else goal
        step = acceleration * dt
        result.append(value + max(-step, min(step, desired - value)))
    return tuple(result)


class ManualVelocityControl:
    def _init_manual_velocity(self):
        self._manual_velocity_handle = None
        self._manual_velocity_connection = None
        self._manual_velocity_current = (0.0, 0.0, 0.0)
        self._manual_velocity_target = (0.0, 0.0, 0.0)
        self._manual_velocity_guard = None
        self._manual_velocity_at = 0.0

    def _cancel_manual_velocity(self):
        active = self._manual_velocity_connection is not None
        if self._manual_velocity_handle is not None:
            self._manual_velocity_handle.cancel()
        self._init_manual_velocity()
        return active

    def _stop_manual_velocity(self):
        if self._manual_velocity_connection is self._conn and self._conn and self.is_connected:
            try:
                self.move_velocity(0, 0, 0)
                return
            except (ValueError, RuntimeError):
                logger.warning('manual velocity stop could not be sent before disconnect')
        self._cancel_manual_velocity()

    def manual_velocity(self, vx, vy, vyaw, *, can_continue, deadman_s=1.0):
        """Only HTTP joystick targets use this ramp; navigation stays unchanged."""
        target = (vx, vy, vyaw)
        self._validate_velocity(*target)
        if not any(target):
            return self.move_velocity(0, 0, 0)
        if not self.is_connected or not self._conn:
            raise RuntimeError('Go2 not connected')
        if not self._move_control_active:
            raise CommandBlocked('请先进入移动控制')
        if not can_continue():
            raise CommandBlocked('control_lease_lost')
        body = self._validate_motion('Move', dict(zip(('x', 'y', 'z'), target)))
        if self._command_lock.locked():
            raise CommandBlocked('command_in_flight')
        loop = asyncio.get_running_loop()
        if self._manual_velocity_connection is None:
            first = slew_velocity((0, 0, 0), target, _INTERVAL_S)
            self.move_velocity(*first)
            self._manual_velocity_current = first
            self._manual_velocity_connection = self._conn
            self._manual_velocity_at = loop.time()
        self._manual_velocity_target = target
        self._manual_velocity_guard = can_continue
        # Only fresh user input renews this deadline. Hardware ticks never do.
        if self._velocity_timer is not None:
            self._velocity_timer.cancel()
        self._velocity_timer = loop.call_later(deadman_s, self._expire_velocity)
        if self._manual_velocity_handle is None:
            self._manual_velocity_handle = loop.call_later(_INTERVAL_S, self._tick_manual_velocity)
        return {'result': 'accepted', 'confirmation': 'transport', 'body_state': body}

    def _tick_manual_velocity(self):
        self._manual_velocity_handle = None
        if not self.is_connected or self._conn is not self._manual_velocity_connection:
            self._cancel_manual_velocity()
            return
        try:
            if not self._move_control_active or not self._manual_velocity_guard():
                raise CommandBlocked('manual_control_ended')
            if self._command_lock.locked():
                raise CommandBlocked('command_in_flight')
            self._validate_motion('Move', dict(zip(('x', 'y', 'z'), self._manual_velocity_target)))
            loop = asyncio.get_running_loop()
            # A stalled event loop must not turn elapsed time into a speed jump.
            dt = min(loop.time() - self._manual_velocity_at, 2 * _INTERVAL_S)
            self._manual_velocity_at = loop.time()
            velocity = slew_velocity(self._manual_velocity_current, self._manual_velocity_target, dt)
            self._publish_velocity(*velocity, sport=True)
            self._manual_velocity_current = velocity
            self._manual_velocity_handle = loop.call_later(_INTERVAL_S, self._tick_manual_velocity)
        except (ValueError, RuntimeError):
            # Losing posture evidence, ownership or transport ends the stream.
            # Stop bypasses the ramp and remains available without body evidence.
            try:
                self.move_velocity(0, 0, 0)
            except (ValueError, RuntimeError):
                self._cancel_manual_velocity()
                logger.warning('manual velocity stop could not be sent')
