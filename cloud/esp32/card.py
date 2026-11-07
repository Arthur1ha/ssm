"""Lamp-specific public skill mapping on top of the ESP32 manifest."""

from cloud.cards.schema import SkillDef
from copy import deepcopy

LIGHTING_GOAL_SCHEMA = {
    "type": "object", "required": ["goal"],
    "properties": {"goal": {"type": "string", "minLength": 1, "maxLength": 1000}},
    "additionalProperties": False,
}


def lighting_goal(params: dict) -> str:
    """The semantic receiver accepts precisely the publicly declared request."""
    if not isinstance(params, dict) or set(params) != {"goal"}:
        raise ValueError("invalid lighting parameters")
    goal = params["goal"]
    spec = LIGHTING_GOAL_SCHEMA["properties"]["goal"]
    if (not isinstance(goal, str) or not spec["minLength"] <= len(goal.strip())
            or len(goal) > spec["maxLength"]):
        raise ValueError("invalid lighting goal")
    return goal.strip()


def adjust_lighting_skill(unit_id: str) -> SkillDef:
    return {
        "id": "adjust_lighting",
        "name": "按目标调整照明",
        "description": "把照明目标交给灯智能体；由灯根据当前感知选择颜色、亮度和动作",
        "tags": ["actuator", "light", "semantic"],
        "params_schema": deepcopy(LIGHTING_GOAL_SCHEMA),
        "invoke": {
            "action": "",
            "transport": {"kind": "http", "endpoint": f"/api/internal/esp32/{unit_id}/agent/tasks"},
        },
    }
