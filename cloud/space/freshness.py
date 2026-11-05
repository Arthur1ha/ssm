"""Choose the observation time for MQTT state without trusting an edge RTC."""

from __future__ import annotations

import math


def state_observed_at(state: dict, now: float) -> float | None:
    """Prefer a live cloud receipt; retained snapshots keep their device time."""
    key = "_received_at" if "_received_at" in state else "ts"
    value = state.get(key)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value > now + 5):
        return None
    return float(value)
