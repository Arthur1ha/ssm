"""Small shared space view assembled from each process's existing MQTT state."""

from __future__ import annotations

import math
import time
from typing import Any

from cloud.cards.registry import CardRegistry, get_registry
from cloud.peers.view import STATE_MAX_AGE_S, list_peers
from cloud.space.constraints import ConstraintStore, get_constraint_store
from cloud.space.activity import get_activity_store

_VALUES = ("ism", "fsm_state", "connected", "battery_soc", "brightness",
           "r", "g", "b", "value", "level", "detected", "presence")
_MAX_UNITS = 24


def _summarize(unit_id: str, entry: Any, now: float) -> dict[str, Any]:
    state = entry.get("state") if isinstance(entry, dict) else None
    if not isinstance(state, dict):
        return {"source": unit_id, "status": "unknown"}
    raw_ts = state.get("ts")
    if (isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float))
            or not math.isfinite(raw_ts) or raw_ts <= 0 or raw_ts > now + 5):
        return {"source": unit_id, "status": "unknown"}
    as_of = float(raw_ts)
    if now - as_of > STATE_MAX_AGE_S:
        return {"source": unit_id, "status": "stale", "as_of": as_of}
    values = {key: state[key] for key in _VALUES if key in state and
              (isinstance(state[key], (str, bool, int)) or
               (isinstance(state[key], float) and math.isfinite(state[key])))}
    return {"source": unit_id, "status": "current", "as_of": as_of,
            "values": values}


def get_space_context(
    unit_id: str, *, state: Any,
    registry: CardRegistry | None = None,
    constraints: ConstraintStore | None = None,
    now: float | None = None,
    include_self: bool = True,
) -> dict[str, Any]:
    """Return peers, recent physical state, and effective user requirements.

    ``state`` is the caller process's existing ESP32State or SharedState.
    No device data is copied into another long-lived registry.
    """
    instant = time.time() if now is None else now
    peer_cards = registry or get_registry()
    requirements = constraints or get_constraint_store()
    peers = (list_peers(peer_cards, initiator_unit_id=unit_id,
                        include_self=include_self, now=instant)
             if callable(getattr(peer_cards, "get_runtime_snapshot", None)) else [])
    environment: list[dict[str, Any]] = []
    snapshots = []
    for method_name in ("sensor_snapshot", "actuator_snapshot"):
        method = getattr(state, method_name, None)
        snapshots.append(method() if callable(method) else {})
    for snapshot in snapshots:
        for source, entry in sorted(snapshot.items()):
            if len(environment) >= _MAX_UNITS:
                break
            environment.append(_summarize(source, entry, instant))
    activity_store = get_activity_store()
    current = activity_store.current(space_id="main")
    activity = None
    if current and current.get("status") in {"starting", "active", "partial"}:
        activity = {key: current[key] for key in
                    ("activity_id", "space_id", "context", "started_at", "revision", "status")
                    if key in current}
    discoveries = (activity_store.recent_discoveries(unit_id, now=instant)
                   if activity and callable(getattr(activity_store, "recent_discoveries", None)) else [])
    return {"peers": peers, "environment": environment,
            "constraints": requirements.list_for(unit_id, now=instant),
            "activity": activity, "recent_discoveries": discoveries}
