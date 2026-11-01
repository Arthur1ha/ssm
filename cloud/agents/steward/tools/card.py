"""Final Card/schema/transport checks before coordinator dispatch."""

import time

from cloud.cards.validation import normalize_card
from cloud.peers.gateway import validate_skill_params
from cloud.space.registry import card_device_id, get_space_registry


def resolve_skill_transport(card: dict, skill: dict) -> dict:
    override = (skill.get("invoke") or {}).get("transport")
    if not override:
        return card.get("transport") or {}
    if card.get("card_schema_version", 0) != 1:
        raise ValueError("legacy_card_override_denied")
    validated = normalize_card(card)
    current = next((item for item in validated.get("skills", [])
                    if item.get("id") == skill.get("id")), None)
    if current is None:
        raise ValueError("skill_not_on_card")
    return (current.get("invoke") or {}).get("transport") or validated["transport"]


def validate_public_skill(card: dict, skill: dict, params: dict, context: dict) -> dict:
    if not card.get("online", False):
        raise PermissionError("target_offline")
    if context.get("space_id", "main") != "main":
        raise PermissionError("invalid_space")
    if "space_id" in context and card.get("agent_type") != "decision":
        device_id = card_device_id(card)
        if device_id not in set(get_space_registry().get_space()["device_ids"]):
            raise PermissionError("target_outside_space")
    actor = context.get("actor", "visitor")
    origin = context.get("origin_kind") or "user_delegated"
    if actor not in {"admin", "visitor", "agent"} or origin not in {
        "user_delegated", "agent_autonomous",
    }:
        raise PermissionError("invalid_execution_context")
    if origin == "agent_autonomous" and (actor != "agent" or context.get("persist")):
        raise PermissionError("autonomous_authority_denied")
    if context.get("persist") and actor != "admin":
        raise PermissionError("persistent_authority_denied")
    expires_at = context.get("expires_at")
    if expires_at is not None and expires_at <= time.time():
        raise PermissionError("expired_context")
    if not isinstance(params, dict):
        raise ValueError("invalid_skill_parameters")
    if not isinstance(skill.get("id"), str) or not skill.get("id"):
        raise ValueError("invalid_skill")
    if "params_schema" in skill:
        validate_skill_params(card, skill["id"], params)
    return resolve_skill_transport(card, skill)
