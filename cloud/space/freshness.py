"""Choose the observation time for MQTT state without trusting an edge RTC."""

from __future__ import annotations

import math

OBSERVATION_MAX_AGE_S = 60.0


def state_observed_at(state: dict, now: float) -> float | None:
    """Prefer a live cloud receipt; retained snapshots keep their device time."""
    key = "_received_at" if "_received_at" in state else "ts"
    value = state.get(key)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0 or value > now + 5):
        return None
    return float(value)


def state_validity(state: dict, now: float, *, online: bool | None = None,
                   connection_confirmed: bool = True) -> dict:
    """Keep online, live-confirmed lighting settings until they change.

    Sampled observations retain their age limit. Retained delivery and command
    acceptance cannot refresh a setting; its original timestamp is preserved.
    """
    observed = state_observed_at(state, now)
    if observed is None:
        return {"status": "unknown"}
    lighting = (isinstance(state.get("ism"), str) and state["ism"] in {"OFF", "ON", "BLINKING"}
                and all(type(state.get(key)) is int and 0 <= state[key] <= 255
                        for key in ("r", "g", "b", "brightness")))
    if (lighting and online is True and "_received_at" in state
            and connection_confirmed and state.get("_connection_confirmed", True)):
        return {"status": "current", "as_of": observed, "validity": "until_changed"}
    invalid_connection = (online is False or state.get("_connection_confirmed") is False
                          or online is True and not connection_confirmed)
    stale = now - observed > OBSERVATION_MAX_AGE_S or lighting and invalid_connection
    return {"status": "stale" if stale else "current", "as_of": observed}


def summary_is_current(summary: dict, now: float) -> bool:
    observed = summary.get("as_of")
    return (summary.get("status") == "current" and type(observed) in (int, float)
            and math.isfinite(observed) and 0 < observed <= now + 5
            and (summary.get("validity") == "until_changed"
                 or now - observed <= OBSERVATION_MAX_AGE_S))
