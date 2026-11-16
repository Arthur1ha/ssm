"""Small, factual step details for the browser's progress display."""

from __future__ import annotations

import math


def step_progress_facts(step: dict) -> dict:
    params = step.get("params") or {}
    result = step.get("result") or {}
    target = params.get("unit_id") or params.get("target_unit_id") or ""
    skill = params.get("skill_id") or ""
    arguments = params.get("params") if skill else params
    if not isinstance(arguments, dict):
        arguments = {}
    # Keep the event useful without forwarding messages, peer lists, or large tool output.
    detail = public_arguments(arguments, tool=step.get("tool") or "", target=target, skill_id=skill)
    requested_text = arguments.get("message") if skill else ""
    if not isinstance(requested_text, str) or len(requested_text) > 120:
        requested_text = ""
    reason = next((result[key] for key in ("message", "error", "reason")
                   if isinstance(result.get(key), str) and result[key]), "")
    nested_results = result.get("tool_results")
    if not reason and isinstance(nested_results, list):
        for item in nested_results:
            nested = item.get("result") if isinstance(item, dict) else None
            if not isinstance(nested, dict) or nested.get("result") == "ok":
                continue
            reason = next((nested[key] for key in ("message", "error", "reason")
                           if isinstance(nested.get(key), str) and nested[key]), "")
            if reason:
                break
    return {"tool": step.get("tool") or "", "target_unit_id": target,
            "skill_id": skill, "arguments": detail,
            "requested_text": requested_text,
            "reason": reason[:160], "result_state": result.get("ism_state") or ""}


def canonical_capability(value: str) -> str:
    """Normalize display identifiers, never commands sent to the device."""
    from cloud.esp32.tool_registry import LAMP_SKILLS

    for action, skill in LAMP_SKILLS.items():
        if value in (action, skill["tool"], skill["id"]):
            return skill["tool"]
    return value


def public_arguments(value: object, *, tool: str = "", target: str = "", skill_id: str = "") -> dict:
    """Bound public scalar arguments; no prompts, credentials or context blobs."""
    if not isinstance(value, dict):
        return {}
    from cloud.esp32.tool_registry import TOOL_DECLARATIONS
    ability = canonical_capability(skill_id or tool)
    schema = (TOOL_DECLARATIONS.get(ability) or {}).get("params_schema")
    if schema is None and ability.startswith("go2_"):
        from cloud.go2.tools.registry import get_tool_spec
        spec = get_tool_spec(ability)
        schema = spec.params_schema if spec else None
    if schema is None and target and skill_id:
        from cloud.cards.registry import get_registry
        card = get_registry().get_card(target) or {}
        skill = next((item for item in card.get("skills", []) if item.get("id") == skill_id), {})
        schema = skill.get("params_schema")
    fields = (schema or {}).get("properties", {})
    private = {"content", "message", "rule", "actor", "persist", "requester", "context",
               "token", "secret", "password", "authorization", "prompt", "api_key",
               "decision_reason", "evidence_refs"}
    return {key: item for key, item in list(value.items())[:32]
            if isinstance(key, str) and key in fields and len(key) <= 64 and key.lower() not in private
            and not any(part in key.lower() for part in ("token", "secret", "password", "credential"))
            and (isinstance(item, (bool, int)) or isinstance(item, float) and math.isfinite(item)
                 or isinstance(item, str) and len(item) <= 120)}


def public_process_facts(value: dict) -> dict:
    """One projection for live agent events and persisted activity details."""
    from cloud.esp32.tool_registry import LAMP_SKILLS

    result = {}
    for key in ("run_id", "parent_run_id", "step_id", "target_unit_id", "skill_id",
                "tool", "action", "requested_text", "result_state"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = canonical_capability(item[:120]) if key in {"action", "tool"} else item[:120]
    if isinstance(value.get("arguments"), dict):
        result["arguments"] = public_arguments(value["arguments"],
            tool=result.get("tool") or result.get("action") or "",
            target=result.get("target_unit_id") or "", skill_id=result.get("skill_id") or "")
    ability = result.get("skill_id") or result.get("tool") or result.get("action")
    canonical = canonical_capability(ability or "")
    for skill in LAMP_SKILLS.values():
        if canonical == skill["tool"]:
            result["capability_name"] = skill["name"]
            break
    if ability and canonical.startswith("go2_") and "capability_name" not in result:
        from cloud.go2.tools.registry import CARD_SKILLS
        skill = CARD_SKILLS.get(canonical)
        if skill:
            result["capability_name"] = skill["name"]
    if ability and "capability_name" not in result:
        from cloud.cards.registry import get_registry
        unit_id = result.get("target_unit_id") or value.get("unit_id") or value.get("agent_id")
        card = get_registry().get_card(unit_id) if unit_id else None
        for skill in (card or {}).get("skills", []):
            if skill.get("id") == ability:
                result["capability_name"] = str(skill.get("name") or ability)[:120]
                break
    return result
