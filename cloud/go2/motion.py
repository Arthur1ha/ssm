"""Physical velocity policy shared by navigation and joystick control.

Targets are body-frame m/s and rad/s. This module has no transport, task or
authority state: callers validate the resulting velocity before publishing it.
"""

import math

LINEAR_ACCELERATION = .6  # m/s², bounds the XY vector change (including turns)
ANGULAR_ACCELERATION = 1.2  # rad/s²
CONTROL_INTERVAL_S = .05
COMMAND_TTL_S = .3
MIN_TURN_TARGET = .35  # target floor; not a measured guarantee of hardware motion


def slew_velocity(current, target, dt):
    """Bound vector acceleration; an explicit all-zero target stops immediately.

    A zero component within a moving target decelerates continuously, so changing
    direction cannot abruptly remove one axis. Interpolation from rest preserves
    the requested XY direction; transitions stay within the larger endpoint norm.
    """
    if not any(target):
        return (0., 0., 0.)
    target = tuple(0. if value * goal < 0 else goal
                   for value, goal in zip(current, target))
    dt = max(0., min(dt, 2 * CONTROL_INTERVAL_S))
    dx, dy = target[0] - current[0], target[1] - current[1]
    change = math.hypot(dx, dy)
    scale = min(1., LINEAR_ACCELERATION * dt / change) if change else 1.
    yaw_step = ANGULAR_ACCELERATION * dt
    return (current[0] + dx * scale, current[1] + dy * scale,
            current[2] + max(-yaw_step, min(yaw_step, target[2] - current[2])))


def turn_velocity(error, limit, tolerance):
    """Brake toward a measured angle while retaining an effective turn input.

    The floor applies to the target, not the acceleration ramp. Short turns must
    not remain at ineffective tiny yaw inputs; all arrivals still need telemetry.
    """
    speed = min(limit, max(min(MIN_TURN_TARGET, limit), math.sqrt(
        2 * ANGULAR_ACCELERATION * max(0., abs(error) - tolerance))))
    return math.copysign(speed, error)


class VelocityProfile:
    """One producer's last *sent* command; a pause cannot inherit old velocity."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.velocity = (0., 0., 0.)
        self.sent_at = None

    def preview(self, target, now):
        current = self.velocity
        if self.sent_at is None or now - self.sent_at >= COMMAND_TTL_S:
            current = (0., 0., 0.)
            dt = CONTROL_INTERVAL_S
        else:
            dt = now - self.sent_at
        return slew_velocity(current, target, dt)

    def sent(self, velocity, now):
        self.velocity, self.sent_at = velocity, now
