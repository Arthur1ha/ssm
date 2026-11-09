"""Collaboration policy. Advertised skills do not grant authority.

The steward carries full user sport/life delegation; device peers retain the
narrower social subset, even when the same skill appears on their Card.
"""

from cloud.go2.actions import SPORT_COMMANDS
from cloud.go2.state import LIFE_ACTIONS


def permits_skill(target: str, skill_id: str, params: dict, origin: str, *,
                  audience: str, activity_authorized: bool = False) -> bool:
    if audience not in {"steward", "peer"} or origin not in {
        "user_delegated", "agent_autonomous",
    }:
        return False
    delegated = origin == "user_delegated"
    if target == "cloud_orchestrator":
        return audience == "peer" and skill_id == "coordinate_request"
    if target == "go2":
        if skill_id == "go2_observe":
            return audience == "peer"
        if skill_id == "go2_sport":
            allowed = set(SPORT_COMMANDS) if delegated and audience == "steward" else {"Hello"}
            if audience == "peer" and (delegated or activity_authorized):
                allowed.update({"Dance1", "Dance2"})
            return params.get("cmd") in allowed
        if skill_id == "go2_life":
            return audience == "steward" and delegated and params.get("action") in LIFE_ACTIONS
        if skill_id == "go2_move":
            from cloud.go2.tools.registry import get_tool_spec
            from cloud.go2.tools.executor import validate_params
            if not delegated:
                return False
            try:
                validate_params(get_tool_spec("go2_move").params_schema, params)
                from cloud.go2.actions import validate_move_goal
                validate_move_goal(params)
            except ValueError:
                return False
            return True
        if skill_id == "go2_visual_navigate":
            return delegated and isinstance(params.get("target"), str) and bool(params["target"].strip())
        if skill_id == "go2_find_object":
            return (delegated and audience == "steward"
                    and isinstance(params.get("target"), str) and bool(params["target"].strip()))
        return False
    if skill_id == "adjust_lighting":
        return delegated and isinstance(params.get("goal"), str) and bool(params["goal"].strip())
    if skill_id == "blink_light":
        return delegated
    return skill_id == "set_light_state" and params.get("state") in {"ON", "OFF"}
