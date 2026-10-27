"""Small, factual step details for the browser's progress display."""

from __future__ import annotations


def step_progress_facts(step: dict) -> dict:
    params = step.get("params") or {}
    result = step.get("result") or {}
    target = params.get("unit_id") or params.get("target_unit_id") or ""
    skill = params.get("skill_id") or ""
    arguments = params.get("params") if skill else params
    if not isinstance(arguments, dict):
        arguments = {}
    # Keep the event useful without forwarding messages, peer lists, or large tool output.
    detail = {key: value for key, value in arguments.items()
              if key not in {"content", "message", "rule"}
              and isinstance(value, (str, int, float, bool))
              and len(str(value)) <= 80}
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
