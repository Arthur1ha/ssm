"""The single declaration of Go2's planner-visible tools and their limits."""

from dataclasses import dataclass, replace
from copy import deepcopy

from cloud.go2.actions import ACTIONS, SPORT_COMMANDS, sport_description, sport_tool_schema, move_tool_schema, led_tool_schema
from cloud.go2.state import life_tool_schema
from cloud.agents.go2.workflows.search import SEARCH_TOOL_TIMEOUT_S

VISUAL_NAVIGATION_TIMEOUT_S = 135.0


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
        ToolSpec("go2_navigate_to_visible_target", "接近当前画面中的目标，target保留具体物品及位置特征。工具自行定位、导航、停稳并复核；仅ok且复核通过才确认安全接近，不表示触碰或精确到达。max_advance_m可限制本次推进米数，arrived=false仅是进展，arrived=true才表示抵达。目标测距不可用时工具可自行降级找物并保留原始回执；outcome=nearby、found=true且stop.result=ok确认已在附近，不表示精确测距。decision_reason可说明本轮观察为何支持候选，游戏中须提供，依据进过程详情，无需额外发言。未知或受阻结果不通过其他移动工具绕过，按原任务查询。", _schema({"target": _string(minLength=1, maxLength=100), "decision_reason": _string(minLength=1, maxLength=300), "max_advance_m": _number(exclusiveMinimum=0)}, ("target",)), motion=True, timeout_s=SEARCH_TOOL_TIMEOUT_S, result_rule="device"),
        ToolSpec("go2_find_object", "在当前定位会话地图的可达范围内寻找target所述地面物品并走到附近，遵守已有范围边界。工具自行选择观察点、靠近和停稳复核，无需用户提供房间名称、坐标或执行步长；仅用户明确指定且无法确定房间时澄清。outcome区分未启动not_started、暂未识别not_seen、见过但未接近seen_not_reached、已接近nearby；nearby且found=true、stop.result=ok表示已在同一目标附近，不代表精确测距或可以触碰。未检查区域不能据此断言没有目标。按message报告实际受阻原因，终态后不承诺自动继续搜索。", _schema({"target": _string(minLength=1, maxLength=100)}, ("target",)), roles=("admin", "visitor"), motion=True, timeout_s=SEARCH_TOOL_TIMEOUT_S, result_rule="device"),
        ToolSpec("go2_push_cargo", "对同一地面货物执行一次必要的接近、站位或普通步态推送；有目的地且货物较远时先接近安全观察位置，停稳后重新观察货物和目的地，再决定换位，到站停稳后对齐并推送，无需逐步指挥或口述站位。绕行参考区不作为硬性禁区，真实障碍检查保留；站位后继续读取新观察。首次确认后由执行器绑定原货物与目的地，后续描述变化或参数省略仍沿用原对象；雷达可关联时直接更新位置。接近和推送均使用完整共享地图，货物与其他障碍统一接受普通机身避障检查。cargo保留位置和外观特征；基础推动测试可省略destination，用户指定目的地时必须传入对应色块、地面标记或物体描述。distance_m省略时按当前目标观察估算；明确提供时为任意有限正数。positioned、aligned、approached仅为前置进展，final=false继续。directional_push_attempted=true且confirmation=directional_move_and_stop、stop.result=ok表示本单方向推送尝试完成，应结束，不追加后退复查或重复推送；这不证明货物实际移动或送达。只有delivered=true且confirmation=cargo_near_destination_and_stopped才确认货物已在目的地附近；motion_performed=false表示本次没有运动。受阻时保留真实原因，不用其他移动工具绕过；未知结果按原request_id核对，不换ID重发。", _schema({"cargo": _string(minLength=1, maxLength=150), "destination": _string(minLength=1, maxLength=150), "distance_m": _number(exclusiveMinimum=0)}, ("cargo",)), motion=True, timeout_s=240, result_rule="device"),
        ToolSpec("go2_list_locations", "列出已保存地点", _schema({}), timeout_s=5),
        ToolSpec("go2_set_led", "设置机身灯颜色和时长", led_tool_schema(), timeout_s=8, result_rule="device"),
        ToolSpec("go2_recognize_person", "识别当前面前的人；匹配分达到80且对应已登记人员才确认身份。status=unknown 表示未匹配登记人员，称为陌生人或其他人；无脸、图像不合格或服务失败按错误说明，不推断身份", _schema({}), timeout_s=32, result_rule="identity"),
        ToolSpec("go2_list_people", "列出空间人员库", _schema({}), roles=("admin",), timeout_s=5),
        ToolSpec("list_peers", "查询当前空间其他智能体的在线状态与公开技能；请求协作前先查询", _schema({}), timeout_s=5),
        ToolSpec("get_space_context", "重新读取当前空间的同伴、环境与有效用户要求", _schema({}), timeout_s=5),
        ToolSpec("send_message", "给同空间在线智能体发送简短消息；已发送不表示对方答应或完成动作", _schema({
            "target": _string(minLength=1, maxLength=64),
            "content": _string(minLength=1, maxLength=1000),
            "reply_to": _string(minLength=1, maxLength=64),
            "interaction_kind": _string(enum=["message", "game_invite"]),
            "game_id": _string(minLength=1, maxLength=64),
            "decision_reason": _string(minLength=1, maxLength=300,
                                       description="游戏邀请必填：依据当前事实简短说明为何此时邀请"),
            "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 8,
                              "items": _string(minLength=1, maxLength=200),
                              "description": "游戏邀请必填：仅引用当前 space.decision_evidence 中真实存在的 ref"},
        }, ("target", "content")), timeout_s=5),
        ToolSpec("request_peer_skill", "请求同伴执行公开技能并等待真实结果；执行是否成功按该技能回执契约判断，消息投递不表示动作完成。失败或结果未知时不执行依赖动作。请求前先 list_peers；超时只用原 request_id 查询，不换新 ID 重发物理动作", _schema({
            "target_unit_id": _string(minLength=1),
            "skill_id": _string(minLength=1),
            "params": {"type": "object"},
            "request_id": _string(minLength=1, maxLength=100),
        }, ("target_unit_id", "skill_id", "params")), timeout_s=155),
    )
}


def target_navigation_complete(result: dict, target: str) -> bool:
    """Accept metric arrival or stopped visual proximity, never mere progress."""
    if result.get('result') != 'ok' or result.get('target') != target:
        return False
    if result.get('confirmation') == 'odom_stopped_and_visual_recheck':
        return True
    fallback = result.get('fallback') or {}
    return bool(result.get('outcome') == 'nearby' and result.get('found') is True
                and (result.get('stop') or {}).get('result') == 'ok'
                and result.get('task_id')
                and fallback.get('tool') == 'go2_find_object'
                and fallback.get('task_id') == result['task_id'])


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


def tool_display_name(name: str) -> str | None:
    """Resolve public names without advertising internal tools as Card skills."""
    entry = CARD_SKILLS.get(name) or next(
        (skill for skill in CARD_SKILLS.values() if skill.get("tool_name") == name), None)
    if entry:
        return entry["name"]
    return {
        "go2_push_cargo": "推送货物",
        "go2_navigate_to_coordinate": "前往指定位置",
        "go2_list_people": "查看空间人员",
    }.get(name)
