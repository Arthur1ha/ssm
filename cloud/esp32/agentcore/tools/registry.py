"""Single source of lamp action parameters for chat, buttons and autonomy."""

from typing import Any

PHYSICAL_ACTIONS = {
    "SET_STATE": {"tool": "set_led_state", "timeout": 20.0},
    "SET_COLOR": {"tool": "set_led_color", "timeout": 20.0},
    "BLINK": {"tool": "blink_led", "timeout": 20.0},
}

TOOL_DECLARATIONS = {
    "set_led_state": {"action": "SET_STATE", "role": "user_or_reactive", "persistent": False,
                      "timeout": 20.0, "result_type": "edge_result",
                      "description": "开关灯；state 只能为 ON 或 OFF",
                      "params_schema": {"type": "object", "required": ["state"],
                                        "properties": {"state": {"enum": ["ON", "OFF"]}}}},
    "set_led_color": {"action": "SET_COLOR", "role": "user_or_reactive", "persistent": False,
                      "timeout": 20.0, "result_type": "edge_result",
                      "description": "设置 RGB 和亮度；四项均为 0-255 整数，省略 brightness 时保留当前值",
                      "params_schema": {"type": "object", "required": ["r", "g", "b"],
                                        "properties": {k: {"type": "integer", "minimum": 0, "maximum": 255}
                                                       for k in ("r", "g", "b", "brightness")}}},
    "blink_led": {"action": "BLINK", "role": "user_or_reactive", "persistent": False,
                  "timeout": 20.0, "result_type": "edge_result",
                  "description": "闪烁；count 为 1-20，颜色三项可选且均为 0-255 整数",
                  "params_schema": {"type": "object", "required": ["count"],
                                    "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 20},
                                                   **{k: {"type": "integer", "minimum": 0, "maximum": 255}
                                                      for k in ("r", "g", "b")}}}},
    "speak": {"role": "user_or_reactive", "persistent": False, "timeout": 10.0,
              "result_type": "accepted", "description": "播报不超过 120 字的中文文本",
              "params_schema": {"type": "object", "required": ["text"],
                                "properties": {"text": {"type": "string", "maxLength": 120}}}},
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
                                                      "reply_to": {"type": "string"}}}},
}

TOOL_DESCRIPTIONS = "可用工具（系统注入目标灯单元 ID）：\n" + "\n".join(
    f"- {name}{' / ' + declaration['action'] if declaration.get('action') else ''}: {declaration['description']}"
    for name, declaration in TOOL_DECLARATIONS.items()
) + "\n无需动作时输出 []"


def valid_auxiliary_params(tool: str, params: Any) -> bool:
    if not isinstance(params, dict):
        return False
    if tool == "speak":
        return set(params) == {"text"} and isinstance(params["text"], str) and 0 < len(params["text"].strip()) <= 120
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
        return (set(params) <= {"target", "content", "reply_to"}
                and isinstance(params.get("target"), str)
                and 0 < len(params["target"]) <= 120
                and isinstance(params.get("content"), str)
                and 0 < len(params["content"].strip()) <= 1000
                and ("reply_to" not in params or
                     (isinstance(params["reply_to"], str) and
                      0 < len(params["reply_to"]) <= 100)))
    return False


def valid_lamp_params(action: str, params: Any, *, allow_legacy: bool = False) -> bool:
    if not isinstance(params, dict):
        return False
    if action == "SET_STATE":
        states = {"ON", "OFF", "BRIGHT", "DIM"} if allow_legacy else {"ON", "OFF"}
        return set(params) == {"state"} and params["state"] in states
    if action == "SET_COLOR":
        required, optional = {"r", "g", "b"}, {"brightness"}
        if not required <= set(params) or set(params) - required - optional:
            return False
        return all(type(value) is int and 0 <= value <= 255 for value in params.values())
    if action == "BLINK":
        if "count" not in params or set(params) - {"count", "r", "g", "b"}:
            return False
        return all(
            type(value) is int and (1 <= value <= 20 if key == "count" else 0 <= value <= 255)
            for key, value in params.items()
        )
    return False


def normalize_brightness(action: str, params: dict, current: dict) -> dict:
    """A missing brightness preserves the current setting for SET_COLOR."""
    if action != "SET_COLOR" or "brightness" in params:
        return params
    brightness = current.get("brightness")
    if type(brightness) is not int or not 0 <= brightness <= 255:
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
