"""Public coordinator Card; internal tools remain private to its task loop."""


def build_card(unit_id: str) -> dict:
    return {
        "unit_id": unit_id,
        "device_id": unit_id,
        "name": "云端编排器",
        "description": "协调空间内智能体任务",
        "agent_type": "decision",
        "online": False,
        "transport": {"kind": "mqtt"},
        "skills": [{
            "id": "coordinate_request",
            "name": "跨设备协调",
            "tags": ["coordination"],
            "params_schema": {
                "type": "object", "required": ["message"],
                "properties": {"message": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            "invoke": {"action": "COORDINATE"},
        }],
        "state": {},
    }


def build_manifest(unit_id: str) -> dict:
    return {
        "unit_id": unit_id,
        "agent_type": "decision",
        "name": "llm_orchestrator",
        "hw_platform": "cloud",
        "topics": {
            "manifest": f"ssm/agents/{unit_id}/manifest",
            "state": f"ssm/agents/{unit_id}/state",
        },
    }
