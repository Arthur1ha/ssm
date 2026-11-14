"""Bounded, space-scoped projections of the steward's existing MQTT cache."""

from __future__ import annotations

import time

from cloud.space.context import _summarize, get_space_context
from cloud.space.decisions import decision_evidence
from cloud.space.freshness import state_observed_at
from cloud.space.registry import card_device_id, get_space_registry

_MAX_UNITS = 24


def cached_observations(state, *, cards: dict, device_ids: set[str], now: float) -> list[dict]:
    """Select one latest observation per admitted unit without refreshing its time."""
    selected = {}
    for bucket in ("sensor", "actuator"):
        snapshot = getattr(state, bucket + "_snapshot", lambda: {})()
        if not isinstance(snapshot, dict):
            continue
        for unit_id, entry in snapshot.items():
            if not isinstance(unit_id, str) or not isinstance(entry, dict):
                continue
            card = cards.get(unit_id)
            if isinstance(card, dict):
                allowed = (card.get("agent_type") == "decision"
                           or card_device_id(card) in device_ids)
            else:
                # Sensors have no Cards. Split the final unit segment so an
                # underscored device_id is checked exactly, not as a prefix.
                parent_id, separator, unit = unit_id.rpartition("_")
                allowed = (unit_id in device_ids
                           or bool(separator and unit and parent_id in device_ids))
            if not allowed:
                continue
            raw = entry.get("state")
            observed_at = state_observed_at(raw, now) if isinstance(raw, dict) else None
            rank = (observed_at if observed_at is not None else -1, bucket == "actuator")
            if unit_id not in selected or rank > selected[unit_id][0]:
                selected[unit_id] = (rank, entry)
    return [_summarize(unit_id, selected[unit_id][1], now)
            for unit_id in sorted(selected)[:_MAX_UNITS]]


def read_cached_state(state, *, registry, now: float | None = None) -> dict:
    """An ok read reports cache access, never confirmation of a device action."""
    instant = time.time() if now is None else now
    cards = registry.get_all_cards() if registry else {}
    device_ids = set(get_space_registry().get_space()["device_ids"])
    return {"result": "ok", "source": "mqtt_cache", "as_of": instant,
            "observations": cached_observations(
                state, cards=cards, device_ids=device_ids, now=instant)}


def steward_space_context(*, state, registry, now: float | None = None) -> dict:
    """Use the same unambiguous cache projection in the prompt and state tools."""
    instant = time.time() if now is None else now
    context = get_space_context("cloud_orchestrator", state=state, registry=registry, now=instant)
    context["environment"] = read_cached_state(state, registry=registry, now=instant)["observations"]
    context["decision_evidence"] = decision_evidence(context, now=instant)
    return context
