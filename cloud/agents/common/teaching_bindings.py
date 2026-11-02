"""Typed, card-checked bindings between observations and agent skills."""

from __future__ import annotations

import re
import time


def validate(binding: dict, card: dict) -> dict:
    """Accept only capabilities the target agent currently advertises."""
    if not isinstance(binding, dict) or set(binding) != {"observation_id", "where", "skill_id", "params"}:
        raise ValueError("binding requires observation_id, where, skill_id and params")
    observations = {item.get("id"): item for item in card.get("observations", [])
                    if isinstance(item, dict)}
    observation = observations.get(binding["observation_id"])
    if observation is None or observation.get("available") is False:
        raise ValueError("observation capability unavailable")
    where = binding["where"]
    fields = observation.get("fields") or {}
    types = {"string": str, "boolean": bool, "integer": int, "number": float}
    if not isinstance(where, dict) or not where or not all(
            key in fields and type(value) is types.get(fields[key]) for key, value in where.items()):
        raise ValueError("observation condition is not supported")
    if binding["observation_id"] == "vision.owner_recognized" and where != {"recognized": True}:
        raise ValueError("owner recognition requires a confirmed owner event")
    skill = next((item for item in card.get("skills", [])
                  if item.get("id") == binding["skill_id"]), None)
    if skill is None:
        raise ValueError("skill capability unavailable")
    if binding["skill_id"] not in card.get("autonomous_skills", []):
        raise ValueError("skill unavailable to autonomous graph")
    params = binding["params"]
    schema = skill.get("params_schema") or {}
    properties = schema.get("properties") or {}
    if not isinstance(params, dict) or not set(schema.get("required") or ()) <= params.keys():
        raise ValueError("required skill parameters missing")
    for key, value in params.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            raise ValueError("unknown skill parameter")
        kind = prop.get("type")
        if (kind == "integer" and type(value) is not int or
                kind == "number" and type(value) not in (int, float) or
                kind == "string" and type(value) is not str or
                "enum" in prop and value not in prop["enum"] or
                "minimum" in prop and value < prop["minimum"] or
                "maximum" in prop and value > prop["maximum"]):
            raise ValueError("invalid skill parameter")
    return {"observation_id": binding["observation_id"], "where": dict(where),
            "skill_id": binding["skill_id"], "params": dict(params)}


def validate_for_teaching(binding: dict, card: dict, trigger: str) -> dict:
    """Keep identity-specific teachings from silently matching every person."""
    checked = validate(binding, card)
    if (card.get("unit_id") == "go2" and
            checked["observation_id"] in {"vision.person_presence", "vision.face_presence"} and
            re.search(r"主人|看到我|看见我|见到我|认出我|识别我", trigger)):
        raise ValueError("identity trigger requires owner recognition observation")
    return checked


def candidates(records: list[dict], observation: dict, card: dict, *, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    if not isinstance(observation, dict):
        return []
    observed_at = observation.get("ts")
    if type(observed_at) not in (int, float) or abs(now - observed_at) > 5:
        return []
    result = []
    for record in records:
        if record.get("execution_status") != "enabled":
            continue
        binding = record.get("binding")
        try:
            checked = validate_for_teaching(binding, card, str(record.get("trigger") or ""))
        except (ValueError, KeyError, TypeError):
            continue
        if (checked["observation_id"] == observation.get("id") and
                all(observation.get("values", {}).get(key) == value
                    for key, value in checked["where"].items())):
            result.append({"teaching_id": record["teaching_id"], "name": record["name"],
                           "behavior": record["behavior"], "binding": checked,
                           "observation": observation})
    return result
