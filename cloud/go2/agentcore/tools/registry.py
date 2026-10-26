"""The single declaration of Go2's planner-visible tools and their limits."""

from dataclasses import dataclass


def _schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


def _string(**extra) -> dict:
    return {"type": "string", **extra}


def _number(**extra) -> dict:
    return {"type": "number", **extra}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params_schema: dict
    roles: tuple[str, ...] = ("admin", "visitor")
    persists: bool = False
    motion: bool = False
    timeout_s: float = 30.0
    result_rule: str = "text_ok"


_SPORT = ["StandUp", "StandDown", "StopMove", "Hello", "Stretch", "Dance1", "Dance2"]
_DIRECTIONS = ["forward", "backward", "left", "right", "turn_left", "turn_right"]

TOOL_REGISTRY: dict[str, ToolSpec] = {
    s.name: s for s in (
        ToolSpec("go2_sport", "执行站立、停止、招呼、伸展或跳舞动作", _schema({"cmd": _string(enum=_SPORT)}, ("cmd",)), motion=True, timeout_s=22, result_rule="device"),
        ToolSpec("go2_move", "按方向、速度和时长定时移动；实际位移未确认", _schema({"direction": _string(enum=_DIRECTIONS), "speed": _number(exclusiveMinimum=0, maximum=0.5, default=0.3), "duration": _number(exclusiveMinimum=0, maximum=10, default=1)}, ("direction",)), motion=True, timeout_s=35, result_rule="device"),
        ToolSpec("go2_observe", "用摄像头回答当前画面问题", _schema({"question": _string(default="描述你看到的场景")}), timeout_s=32),
        ToolSpec("go2_status", "查询设备连接和姿态", _schema({}), timeout_s=3),
        ToolSpec("go2_add_rule", "保存视觉触发动作规则", _schema({"trigger": _string(minLength=1), "action": _string(enum=_SPORT), "cooldown_s": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 30}, "behavior": _string(default="")}, ("trigger", "action")), persists=True, timeout_s=5),
        ToolSpec("go2_list_rules", "列出视觉触发规则", _schema({}), timeout_s=5),
        ToolSpec("go2_tag_location", "保存当前地点名称", _schema({"name": _string(minLength=1)}, ("name",)), persists=True, timeout_s=5),
        ToolSpec("go2_navigate_to", "导航到已保存的命名地点", _schema({"name": _string(minLength=1)}, ("name",)), motion=True, timeout_s=27, result_rule="device"),
        ToolSpec("go2_list_locations", "列出已保存地点", _schema({}), timeout_s=5),
        ToolSpec("go2_set_led", "设置机身灯颜色和时长", _schema({"color": _string(enum=["white", "red", "yellow", "blue", "green", "cyan", "purple"], default="white"), "duration": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 60}}), timeout_s=8, result_rule="device"),
        ToolSpec("go2_recognize_person", "识别当前面前的人", _schema({}), timeout_s=32, result_rule="identity"),
        ToolSpec("go2_list_people", "列出空间人员库", _schema({}), roles=("admin",), timeout_s=5),
        ToolSpec("list_peers", "查询当前空间其他智能体的在线状态与公开技能；请求协作前先查询", _schema({}), timeout_s=5),
        ToolSpec("get_space_context", "重新读取当前空间的同伴、环境与有效用户要求", _schema({}), timeout_s=5),
        ToolSpec("send_message", "给同空间在线智能体发送简短消息；已发送不表示对方答应或完成动作", _schema({
            "target": _string(minLength=1, maxLength=64),
            "content": _string(minLength=1, maxLength=1000),
            "reply_to": _string(minLength=1, maxLength=64),
        }, ("target", "content")), timeout_s=5),
        ToolSpec("request_peer_skill", "请求同伴执行公开技能，等待最终结果；只有 ok 表示目标完成，失败后不要执行依赖动作", _schema({
            "target_unit_id": _string(minLength=1),
            "skill_id": _string(minLength=1),
            "params": {"type": "object"},
            "request_id": _string(minLength=1, maxLength=100),
        }, ("target_unit_id", "skill_id", "params")), timeout_s=35),
    )
}


def get_tool_spec(name: str) -> ToolSpec | None:
    return TOOL_REGISTRY.get(name)


def tool_descriptions() -> str:
    return "可用工具：\n" + "\n".join(
        f"- {spec.name}: {spec.description}; 参数 JSON Schema: {spec.params_schema}"
        for spec in TOOL_REGISTRY.values()
    )
