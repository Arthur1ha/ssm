"""Request timing for the shared teaching camera consumer, without another loop."""

import math
import os


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) and value > 0 else default
    except ValueError:
        return default


class TargetObservationSchedule:
    """Monotonic request budget; force requests still obey every hard limit."""

    def __init__(self, *, min_interval_s=3.0, fallback_s=30.0, retry_s=10.0):
        self.min_interval_s = min_interval_s
        self.fallback_s = fallback_s
        self.retry_s = retry_s
        self.last_started = None
        self.last_success = None
        self.retry_after = None
        self.in_flight = False
        self.force_pending = True

    @classmethod
    def from_environment(cls):
        return cls(
            min_interval_s=_seconds('GO2_TRAINING_VISION_MIN_INTERVAL_S', 3.0),
            fallback_s=_seconds('GO2_TRAINING_VISION_FALLBACK_S', 30.0),
            retry_s=_seconds('GO2_TRAINING_VISION_RETRY_S', 10.0))

    def request(self):
        self.force_pending = True

    def due(self, now: float, *, changed=False, refresh=False) -> bool:
        if (self.in_flight
                or self.last_started is not None and now - self.last_started < self.min_interval_s
                or self.retry_after is not None and now < self.retry_after):
            return False
        return (self.force_pending or changed or refresh or self.last_success is None
                or now - self.last_success >= self.fallback_s)

    def start(self, now: float):
        self.in_flight = True
        self.last_started = now
        self.force_pending = False

    def succeed(self, now: float):
        self.in_flight = False
        self.last_success = now
        self.retry_after = None

    def fail(self, now: float):
        self.in_flight = False
        self.retry_after = now + self.retry_s
        self.force_pending = True

    def cancel(self):
        self.in_flight = False

