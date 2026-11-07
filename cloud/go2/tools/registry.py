"""The single declaration of Go2's planner-visible tools and their limits."""

from dataclasses import dataclass, replace
from copy import deepcopy

from cloud.go2.actions import ACTIONS, SPORT_COMMANDS, sport_description, sport_tool_schema, move_tool_schema, led_tool_schema
from cloud.go2.state import life_tool_schema
from cloud.agents.go2.workflows.search import SEARCH_TOOL_TIMEOUT_S


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


TOOL_REGISTRY: dict[str, ToolSpec] = {
    s.name: s for s in (
        ToolSpec("go2_sport", sport_description(), sport_tool_schema(), motion=True, timeout_s=max(ACTIONS[cmd].timeout_s for cmd in SPORT_COMMANDS) + 10, result_rule="device"),
        ToolSpec("go2_move", "按方向、速度和时长定时移动；实际位移未确认", move_tool_schema(), motion=True, timeout_s=35, result_rule="device"),
        ToolSpec("go2_observe", "用摄像头回答当前画面问题", _schema({"question": _string(default="描述你看到的场景")}), timeout_s=32),
        ToolSpec("go2_life", "休息保持连接和姿态；唤醒回待命；开启探索需管理员，停止探索不重放旧任务", life_tool_schema(), timeout_s=8, result_rule="device"),
        ToolSpec("go2_status", "查询设备连接和姿态", _schema({}), timeout_s=3),
        ToolSpec("go2_list_teachings", "列出已保存教导及自动执行状态", _schema({}), timeout_s=5),
        ToolSpec("go2_tag_location", "保存当前地点名称", _schema({"name": _string(minLength=1)}, ("name",)), persists=True, timeout_s=5),
        ToolSpec("go2_navigate_to", "导航到已保存的命名地点", _schema({"name": _string(minLength=1)}, ("name",)), motion=True, timeout_s=70, result_rule="device"),
        ToolSpec("go2_navigate_to_coordinate", "导航到当前 odom 会话内的临时坐标", _schema({"x": {"type": "number"}, "y": {"type": "number"}}, ("x", "y")), motion=True, timeout_s=70, result_rule="device"),
        ToolSpec("go2_navigate_to_visible_target", "视觉识别目标并与雷达点云配对，在地图上规划到目标附近的停靠点，停稳后重新观察确认", _schema({"target": _string(minLength=1, maxLength=100)}, ("target",)), motion=True, timeout_s=135, result_rule="device"),
        ToolSpec("go2_find_object", "在已配置的单房间范围内持续换观察点寻找地面物品，跨帧确认后定位并接近；需实测相机标定，未检查区域不代表目标不存在", _schema({"target": _string(minLength=1, maxLength=100)}, ("target",)), roles=("admin", "visitor"), motion=True, timeout_s=SEARCH_TOOL_TIMEOUT_S, result_rule="device"),
        ToolSpec("go2_list_locations", "列出已保存地点", _schema({}), timeout_s=5),
        ToolSpec("go2_set_led", "设置机身灯颜色和时长", led_tool_schema(), timeout_s=8, result_rule="device"),
        ToolSpec("go2_recognize_person", "识别当前面前的人", _schema({}), timeout_s=32, result_rule="identity"),
        ToolSpec("go2_list_people", "列出空间人员库", _schema({}), roles=("admin",), timeout_s=5),
        ToolSpec("list_peers", "查询当前空间其他智能体的在线状态与公开技能；请求协作前先查询", _schema({}), timeout_s=5),
        ToolSpec("get_space_context", "重新读取当前空间的同伴、环境与有效用户要求", _schema({}), timeout_s=5),
        ToolSpec("send_message", "给同空间在线智能体发送简短消息；已发送不表示对方答应或完成动作", _schema({
            "target": _string(minLength=1, maxLength=64),
            "content": _string(minLength=1, maxLength=1000),
            "reply_to": _string(minLength=1, maxLength=64),
            "interaction_kind": _string(enum=["message", "game_invite"]),
            "game_id": _string(minLength=1, maxLength=64),
        }, ("target", "content")), timeout_s=5),
        ToolSpec("request_peer_skill", "请求同伴执行公开技能，等待最终结果；只有 ok 表示目标完成，失败后不要执行依赖动作", _schema({
            "target_unit_id": _string(minLength=1),
            "skill_id": _string(minLength=1),
            "params": {"type": "object"},
            "request_id": _string(minLength=1, maxLength=100),
        }, ("target_unit_id", "skill_id", "params")), timeout_s=155),
    )
}


def get_tool_spec(name: str) -> ToolSpec | None:
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return None
    if name == "go2_sport":
        return replace(spec, description=sport_description(), params_schema=sport_tool_schema(),
                       timeout_s=max(ACTIONS[cmd].timeout_s for cmd in SPORT_COMMANDS) + 10)
    if name == "go2_move":
        return replace(spec, params_schema=move_tool_schema())
    if name == "go2_set_led":
        return replace(spec, params_schema=led_tool_schema())
    if name == "go2_life":
        return replace(spec, params_schema=life_tool_schema())
    return spec


def tool_descriptions() -> str:
    return "可用工具：\n" + "\n".join(
        f"- {spec.name}: {spec.description}; 参数 JSON Schema: {spec.params_schema}"
        for spec in (get_tool_spec(name) for name in TOOL_REGISTRY)
    )


CHAT_PARAMS_SCHEMA = _schema({"message": _string(minLength=1, maxLength=2000)}, ("message",))

# Only these externally meaningful goals are advertised to the orchestrator.
# Internal tools and autonomous modes remain private to the Go2 agent.
CARD_SKILLS = {
    "go2_chat": {
        "tool_name": None,
        "name": "对话 / 自然语言控制",
        "description": "通过自然语言与机器狗对话或下达任务",
        "tags": ["conversation", "robot"],
        "invoke": {"action": "CHAT"},
    },
    "go2_sport": {
        "tool_name": "go2_sport",
        "name": "预定义动作",
        "tags": ["motion", "robot"],
        "invoke": {"action": "SPORT"},
    },
    "go2_life": {
        "tool_name": "go2_life",
        "name": "休息、唤醒与探索",
        "tags": ["activity", "robot"],
        "invoke": {"action": "LIFE"},
    },
    "go2_navigate": {
        "tool_name": "go2_navigate_to",
        "name": "导航到命名地点",
        "tags": ["navigation", "robot"],
        "invoke": {"action": "NAVIGATE"},
    },
    "go2_visual_navigate": {
        "tool_name": "go2_navigate_to_visible_target",
        "name": "视觉目标导航",
        "tags": ["navigation", "vision", "motion", "robot"],
        "invoke": {"action": "VISUAL_NAVIGATE"},
    },
    "go2_find_object": {
        "tool_name": "go2_find_object",
        "name": "单房间找物并接近",
        "tags": ["search", "navigation", "vision", "motion", "robot"],
        "invoke": {"action": "FIND_OBJECT"},
    },
    "go2_recognize_person": {
        "tool_name": "go2_recognize_person",
        "name": "识别面前的人",
        "tags": ["vision", "identity", "robot"],
        "invoke": {"action": "RECOGNIZE_PERSON"},
    },
    "go2_move": {
        "tool_name": "go2_move",
        "name": "短距离定时移动",
        "tags": ["motion", "robot"],
        "invoke": {"action": "MOVE"},
    },
    "go2_observe": {
        "tool_name": "go2_observe",
        "name": "观察当前画面",
        "tags": ["vision", "robot"],
        "invoke": {"action": "OBSERVE"},
    },
    "go2_status": {
        "tool_name": "go2_status",
        "name": "查看机器狗状态",
        "tags": ["status", "robot"],
        "invoke": {"action": "STATUS"},
    },
    "go2_set_led": {
        "tool_name": "go2_set_led",
        "name": "设置机身灯",
        "tags": ["light", "robot"],
        "invoke": {"action": "SET_LED"},
    },
    "go2_list_locations": {
        "tool_name": "go2_list_locations",
        "name": "查看已保存地点",
        "tags": ["navigation", "robot"],
        "invoke": {"action": "LIST_LOCATIONS"},
    },
    "go2_tag_location": {
        "tool_name": "go2_tag_location",
        "name": "保存当前地点",
        "tags": ["navigation", "persistent", "robot"],
        "invoke": {"action": "TAG_LOCATION"},
    },
    "go2_list_teachings": {
        "tool_name": "go2_list_teachings",
        "name": "查看已保存教导",
        "tags": ["memory", "robot"],
        "invoke": {"action": "LIST_TEACHINGS"},
    },
}


def get_card_skill(skill_id: str) -> dict | None:
    """Return one public entry, including the matching tool's live schema."""
    entry = CARD_SKILLS.get(skill_id)
    if entry is None:
        return None
    result = deepcopy(entry)
    result["id"] = skill_id
    tool_name = result["tool_name"]
    if tool_name is None:
        result["params_schema"] = deepcopy(CHAT_PARAMS_SCHEMA)
    else:
        spec = get_tool_spec(tool_name)
        if spec is None:
            raise RuntimeError(f"Card skill {skill_id} references missing tool {tool_name}")
        result["params_schema"] = deepcopy(spec.params_schema)
        result["description"] = spec.description
    return result


def card_skills() -> list[dict]:
    return [
        {key: value for key, value in get_card_skill(skill_id).items() if key != "tool_name"}
        for skill_id in CARD_SKILLS
    ]
