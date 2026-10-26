"""Lamp-specific public skill mapping on top of the ESP32 manifest."""

from cloud.cards.schema import SkillDef


def adjust_lighting_skill(unit_id: str) -> SkillDef:
    return {
        "id": "adjust_lighting",
        "name": "按目标调整照明",
        "description": "把照明目标交给灯智能体；由灯根据当前感知选择颜色、亮度和动作",
        "tags": ["actuator", "light", "semantic"],
        "params_schema": {
            "type": "object", "required": ["goal"],
            "properties": {"goal": {"type": "string", "minLength": 1, "maxLength": 1000}},
            "additionalProperties": False,
        },
        "invoke": {
            "action": "",
            "transport": {"kind": "http", "endpoint": f"/api/internal/esp32/{unit_id}/agent/tasks"},
        },
    }
