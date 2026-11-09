"""Public coordinator Card; internal tools remain private to its task loop."""


def build_card(unit_id: str) -> dict:
    return {
        "card_schema_version": 1,
        "version": "1",
        "unit_id": unit_id,
        "device_id": unit_id,
        "name": "云端编排器",
        "description": "协调空间内智能体任务",
        "agent_type": "decision",
        "space_id": "main",
        "transport": {"kind": "mqtt", "task_topic": "ssm/intent/{session_id}"},
        "skills": [{
            "id": "coordinate_request",
            "name": "跨设备协调",
            "description": "理解用户目标并协调空间内智能体执行",
            "tags": ["coordination"],
            "params_schema": {
                "type": "object", "required": ["message"],
                "properties": {"message": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "invoke": {"action": "COORDINATE"},
        }],
    }
