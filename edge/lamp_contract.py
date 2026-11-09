# Pure MicroPython-compatible lamp wire contract. No hardware or cloud imports.
# The current contract supports continuous and explicitly counted blinking.

FSM_VERSION = 3
BYTE_MIN = 0
BYTE_MAX = 255
BLINK_MIN = 0
BLINK_MAX = 20
BLINK_DEFAULT = 0  # Continuous until another lighting command; positive = cycles.

ACTION_SCHEMAS = {
    "SET_STATE": {
        "type": "object", "required": ["state"],
        "properties": {"state": {"type": "string", "enum": ["ON", "OFF"]}},
        "additionalProperties": False,
    },
    "SET_COLOR": {
        "type": "object", "required": ["r", "g", "b"],
        "properties": {}, "additionalProperties": False,
    },
    "BLINK": {
        "type": "object", "required": ["count"],
        "properties": {"count": {"type": "integer", "minimum": BLINK_MIN,
                                  "maximum": BLINK_MAX,
                                  "description": "0 表示持续闪烁，直到切换灯光；正整数表示闪烁次数"}},
        "additionalProperties": False,
    },
}
for _action, _keys in (("SET_COLOR", ("r", "g", "b", "brightness")),
                       ("BLINK", ("r", "g", "b"))):
    for _key in _keys:
        ACTION_SCHEMAS[_action]["properties"][_key] = {
            "type": "integer", "minimum": BYTE_MIN, "maximum": BYTE_MAX,
        }


def capabilities():
    result = []
    for action in ("SET_COLOR", "SET_STATE", "BLINK"):
        schema = ACTION_SCHEMAS[action]
        result.append({"action": action, "params_schema": schema})
    return result


def valid_params(action, params, allow_defaults=False, schemas=None):
    """Check the same limits on cloud and edge; edge retains wire defaults."""
    schema = (ACTION_SCHEMAS if schemas is None else schemas).get(action)
    if schema is None or not isinstance(params, dict):
        return False
    for key in schema.get("required", ()):
        if key not in params and not (allow_defaults and action != "SET_STATE"):
            return False
    for key, value in params.items():
        definition = schema["properties"].get(key)
        if definition is None:
            return False
        if definition["type"] == "integer":
            if type(value) is not int or not definition["minimum"] <= value <= definition["maximum"]:
                return False
        elif not isinstance(value, str) or value not in definition["enum"]:
            return False
    return True
