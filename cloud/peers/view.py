"""Project the retained card/status/state registry into a bounded peer view."""

from __future__ import annotations

import math
import time
from typing import Any

from cloud.cards.registry import CardRegistry
from cloud.space.registry import SpaceRegistry, card_device_id, get_space_registry

STATE_MAX_AGE_S = 60.0
_STATE_FIELDS = ("ism", "fsm_state", "connected", "battery_soc", "brightness")


def _state_summary(state: Any, now: float) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"status": "unknown"}
    raw_ts = state.get("ts")
    if isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float)) or not math.isfinite(raw_ts):
        return {"status": "unknown"}
    as_of = float(raw_ts)
    if as_of <= 0 or as_of > now + 5:
        return {"status": "unknown"}
    if now - as_of > STATE_MAX_AGE_S:
        return {"status": "stale", "as_of": as_of}
    summary = {"status": "current", "as_of": as_of}
    for key in _STATE_FIELDS:
        value = state.get(key)
        if isinstance(value, (str, bool, int)) or (isinstance(value, float) and math.isfinite(value)):
            summary[key] = value
    return summary


def list_peers(
    registry: CardRegistry,
    space_registry: SpaceRegistry | None = None,
    *,
    initiator_unit_id: str,
    include_self: bool = False,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """List public peers for one member of the configured space.

    Membership and the internal decision node are projected here; caller identity
    must be checked by the trusted request gateway before exposing this view.
    """
    instant = time.time() if now is None else now
    space = (space_registry or get_space_registry()).get_space()
    device_ids = set(space["device_ids"])
    cards, status_times = registry.get_runtime_snapshot()
    initiator = cards.get(initiator_unit_id)
    is_decision = bool(initiator and initiator.get("agent_type") == "decision")
    if not is_decision and not (initiator and card_device_id(initiator) in device_ids):
        return []

    peers: list[dict[str, Any]] = []
    for unit_id, card in sorted(cards.items()):
        if not include_self and unit_id == initiator_unit_id:
            continue
        device_id = card_device_id(card)
        agent_type = card.get("agent_type")
        if agent_type == "sensor" or (agent_type != "decision" and device_id not in device_ids):
            continue
        status_id = device_id if device_id in status_times else unit_id
        observed_at = status_times.get(status_id)
        peers.append({
            "unit_id": unit_id,
            "device_id": device_id,
            "name": card.get("name") or unit_id,
            "agent_type": agent_type,
            "online": bool(card.get("online")) and observed_at is not None,
            "skills": [
                {key: skill[key] for key in ("id", "name", "tags", "params_schema") if key in skill}
                for skill in card.get("skills", []) if isinstance(skill, dict)
            ],
            "state_summary": _state_summary(card.get("state"), instant),
            "observed_at": observed_at,
        })
    return peers
