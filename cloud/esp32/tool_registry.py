"""Single source of lamp action parameters for chat, buttons and autonomy."""

from typing import Any

from copy import deepcopy
from edge.lamp_contract import ACTION_SCHEMAS, valid_params

PHYSICAL_TIMEOUT = 20.0

# Frozen v1 wire schema. Current firmware edits must not silently rewrite the
# capabilities of deployed firmware which has no fsm_version field.
LEGACY_ACTION_SCHEMAS = {
    "SET_STATE": {"type": "object", "required": ["state"],
                  "properties": {"state": {"type": "string", "enum": ["BRIGHT", "DIM", "OFF"]}},
                  "additionalProperties": False},
    "SET_COLOR": {"type": "object", "required": ["r", "g", "b"],
                  "properties": {key: {"type": "integer", "minimum": 0, "maximum": 255}
                                 for key in ("r", "g", "b", "brightness")},
                  "additionalProperties": False},
    "BLINK": {"type": "object", "required": ["count"],
              "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 20},
                             **{key: {"type": "integer", "minimum": 0, "maximum": 255}
                                for key in ("r", "g", "b")}},
              "additionalProperties": False},
}

# Public subset and its tool association live here; hardware argument limits
# are the versioned firmware contract, consumed by the receiver as well.
LAMP_SKILLS = {
    "SET_STATE": {"tool": "set_led_state", "id": "set_light_state", "name": "开关灯",
                  "description": "切换灯环的照明状态"},
    "SET_COLOR": {"tool": "set_led_color", "id": "set_light_color", "name": "设置灯光颜色",
                  "description": "设置 RGB 和亮度；省略亮度时保留当前值"},
    "BLINK": {"tool": "blink_led", "id": "blink_light", "name": "闪烁灯光",
              "description": "闪烁指定次数，恢复先前灯光后确认完成"},
}
PHYSICAL_ACTIONS = {
    action: {"tool": metadata["tool"], "timeout": PHYSICAL_TIMEOUT}
    for action, metadata in LAMP_SKILLS.items()
}

TOOL_DECLARATIONS = {
    metadata["tool"]: {
        "action": action, "role": "user_or_reactive", "persistent": False,
        "timeout": PHYSICAL_ACTIONS[action]["timeout"], "result_type": "edge_result",
        "description": metadata["description"], "params_schema": ACTION_SCHEMAS[action],
    }
    for action, metadata in LAMP_SKILLS.items()
}
TOOL_DECLARATIONS.update({
    "list_peers": {"role": "user_or_reactive", "persistent": False, "timeout": 3.0,
                   "result_type": "snapshot", "description": "查询同空间在线同伴和公开技能",
                   "params_schema": {"type": "object", "properties": {}}},
    "get_space_context": {"role": "user_or_reactive", "persistent": False, "timeout": 3.0,
                          "result_type": "snapshot", "description": "重新读取同伴、环境和有效用户要求",
                          "params_schema": {"type": "object", "properties": {}}},
    "request_peer_skill": {"role": "user_or_reactive", "persistent": False,
                           "timeout": 30.0, "result_type": "peer_result",
                           "description": "请求同伴公开技能；实际结果 ok 后才继续依赖动作",
                           "params_schema": {"type": "object",
                                             "required": ["target_unit_id", "skill_id", "params"],
                                             "properties": {"target_unit_id": {"type": "string"},
                                                            "skill_id": {"type": "string"},
                                                            "params": {"type": "object"},
                                                            "request_id": {"type": "string"}}}},
    "send_message": {"role": "user_or_reactive", "persistent": False,
                     "timeout": 3.0, "result_type": "message_delivery",
                     "description": "给同空间智能体发一条短消息；投递成功不等于对方答应或完成动作",
                     "params_schema": {"type": "object", "required": ["target", "content"],
                                       "properties": {"target": {"type": "string"},
                                                      "content": {"type": "string", "maxLength": 1000},
                                                      "reply_to": {"type": "string"},
                                                      "interaction_kind": {"type": "string", "enum": ["message", "game_invite"]},
                                                      "game_id": {"type": "string"}}}},
})

TOOL_DESCRIPTIONS = "可用工具（系统注入目标灯单元 ID）：\n" + "\n".join(
    f"- {name}{' / ' + declaration['action'] if declaration.get('action') else ''}: {declaration['description']}"
    for name, declaration in TOOL_DECLARATIONS.items()
) + "\n无需动作时输出 []"


def valid_auxiliary_params(tool: str, params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if tool in {"list_peers", "get_space_context"}:
        return not params
    if tool == "request_peer_skill":
        return (set(params) <= {"target_unit_id", "skill_id", "params", "request_id"}
                and isinstance(params.get("target_unit_id"), str)
                and bool(params["target_unit_id"])
                and isinstance(params.get("skill_id"), str)
                and bool(params["skill_id"])
                and isinstance(params.get("params"), dict)
                and ("request_id" not in params or isinstance(params["request_id"], str)))
    if tool == "send_message":
        return (set(params) <= {"target", "content", "reply_to", "interaction_kind", "game_id"}
                and isinstance(params.get("target"), str)
                and 0 < len(params["target"]) <= 120
                and isinstance(params.get("content"), str)
                and 0 < len(params["content"].strip()) <= 1000
                and params.get("interaction_kind", "message") in {"message", "game_invite"}
                and ((params.get("interaction_kind", "message") == "game_invite"
                      and isinstance(params.get("game_id"), str)) or
                     (params.get("interaction_kind", "message") == "message"
                      and params.get("game_id") is None))
                and ("reply_to" not in params or
                     (isinstance(params["reply_to"], str) and
                      0 < len(params["reply_to"]) <= 100)))
    return False


def lamp_skill(action: str, *, legacy: bool = False) -> dict:
    metadata = LAMP_SKILLS[action]
    schema = deepcopy(LEGACY_ACTION_SCHEMAS[action] if legacy else
                      TOOL_DECLARATIONS[metadata["tool"]]["params_schema"])
    return {"id": metadata["id"], "name": metadata["name"],
            "description": metadata["description"], "tags": ["actuator", "light"],
            "params_schema": schema, "invoke": {"action": action}}


def valid_lamp_params(action: str, params: Any, *, allow_legacy: bool = False) -> bool:
    return valid_params(action, params, schemas=LEGACY_ACTION_SCHEMAS if allow_legacy else ACTION_SCHEMAS)


def normalize_brightness(action: str, params: dict, current: dict) -> dict:
    """A missing brightness preserves the current setting for SET_COLOR."""
    if action != "SET_COLOR" or "brightness" in params:
        return params
    brightness = current.get("brightness")
    definition = ACTION_SCHEMAS["SET_COLOR"]["properties"]["brightness"]
    if (type(brightness) is not int or
            not definition["minimum"] <= brightness <= definition["maximum"]):
        raise ValueError("current brightness is unavailable")
    return {**params, "brightness": brightness}


def fsm_allows(card: dict, current_ism: str, action: str, params: dict) -> bool:
    """Precheck declared transitions; the edge ISM remains final authority."""
    machine = card.get("state_machine") or {}
    transitions = machine.get("transitions") or []
    if not transitions:
        return False
    for edge in transitions:
        if edge.get("src") != current_ism or edge.get("action") != action:
            continue
        if action == "SET_STATE" and (edge.get("params") or {}).get("state") != params.get("state"):
            continue
        return True
    return False
