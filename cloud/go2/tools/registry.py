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
        ToolSpec("go2_move", "按里程计闭环执行相对移动：前进、后退、左移或右移提供 distance_m；原地左转或右转提供 angle_deg。speed 是可选速度上限。到量后停稳才完成，受阻返回实际位移和原因；不接受 duration，不把米数换算成时间，不把步数猜成米数。缺少距离或转角时向用户澄清。结果中的 distance_m/angle_deg 是实测量，requested_* 是目标量，stop.result=ok 才确认停稳；未完成时保留目标量和受阻原因，motion_not_observed 不代表从未移动", move_tool_schema(), motion=True, timeout_s=70, result_rule="device"),
        ToolSpec("go2_observe", "用摄像头回答当前画面问题", _schema({"question": _string(default="描述你看到的场景")}), timeout_s=32),
        ToolSpec("go2_life", "休息保持连接和姿态；唤醒回待命；开启探索需管理员，停止探索不重放旧任务", life_tool_schema(), timeout_s=8, result_rule="device"),
        ToolSpec("go2_status", "查询设备连接和姿态", _schema({}), timeout_s=3),
        ToolSpec("go2_tag_location", "保存当前地点名称", _schema({"name": _string(minLength=1)}, ("name",)), persists=True, timeout_s=5),
        ToolSpec("go2_navigate_to", "导航到已保存的命名地点", _schema({"name": _string(minLength=1)}, ("name",)), motion=True, timeout_s=70, result_rule="device"),
        ToolSpec("go2_navigate_to_coordinate", "导航到当前 odom 会话内的临时坐标", _schema({"x": {"type": "number"}, "y": {"type": "number"}}, ("x", "y")), motion=True, timeout_s=70, result_rule="device"),
        ToolSpec("go2_navigate_to_visible_target", "朝当前画面中的目标接近，target 传自然语言描述。视觉识别后与雷达点云配对，规划停靠点，停稳后复核；仅 ok 且复核通过表示安全接近，不代表触碰或精确到达。decision_reason可用第一人称简短说明依据刚才观察为何选择这个候选，不声称已经到达；游戏中必须提供。失败直接报告原因，不用相对移动或坐标导航盲走替代", _schema({"target": _string(minLength=1, maxLength=100), "decision_reason": _string(minLength=1, maxLength=300)}, ("target",)), motion=True, timeout_s=135, result_rule="device"),
        ToolSpec("go2_find_object", "在室内寻找地面物品并走到附近；用户任务默认搜索当前定位会话地图的可达区域，已有矩形边界则遵守边界，无需用户提供房间名称或坐标。未发现时换观察点；发现后按可通行路径朝目标方向分段移动，停稳后再看。连续画面粗略判断已在附近并停稳即完成，不要求精确目标坐标或测距。结果不代表可直接触碰目标；未检查区域不代表目标不存在。仅用户明确指定且无法确定房间时澄清。outcome 区分未启动 not_started、暂未识别 not_seen、见过但未接近 seen_not_reached、已接近 nearby；按 message 保留受阻原因。内部范围配置或定位会话问题不要求用户补填房间；终态后不承诺自动继续搜索", _schema({"target": _string(minLength=1, maxLength=100)}, ("target",)), roles=("admin", "visitor"), motion=True, timeout_s=SEARCH_TOOL_TIMEOUT_S, result_rule="device"),
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
        ToolSpec("request_peer_skill", "请求同伴执行公开技能，等待最终结果；只有 ok 表示目标完成，失败后不要执行依赖动作。请求前先 list_peers；超时只用原 request_id 查询，不换新 ID 重发物理动作", _schema({
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
        "name": "按距离移动或按角度转向",
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
