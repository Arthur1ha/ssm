"""cloud.cards.builder — 将 ESP32 manifest 或自描述 card payload 组装成 AgentCard。

构建逻辑：
- build_card_from_manifest：从 MQTT manifest 字典构建（ESP32 设备）。
- parse_card：从自描述 card payload 构建（Go2 等 HTTP 设备，Task 2 接入）。
"""

from __future__ import annotations

from cloud.cards.schema import AgentCard, SkillDef, StateMachine, Transport
from cloud.cards.validation import normalize_card


# ── 声明式技能映射表 ──────────────────────────────────────────────
# 将 ESP32 manifest.capabilities 中的每条 action 映射到完整 SkillDef 结构。
# 新增智能体 action 只需在此表添加一条，build_card_from_manifest 无需修改。
CAPABILITY_SKILLS: dict[str, SkillDef] = {
    "SET_COLOR": {
        "id": "set_light_color",
        "name": "设置灯光颜色",
        "description": "设置灯环 RGB 颜色和亮度",
        "tags": ["actuator", "light"],
        "params_schema": {
            "type": "object",
            "required": ["r", "g", "b"],
            "properties": {
                "r":          {"type": "integer", "minimum": 0, "maximum": 255, "description": "红色通道"},
                "g":          {"type": "integer", "minimum": 0, "maximum": 255, "description": "绿色通道"},
                "b":          {"type": "integer", "minimum": 0, "maximum": 255, "description": "蓝色通道"},
                "brightness": {"type": "integer", "minimum": 0, "maximum": 255,  "description": "亮度，0-255 整数（与硬件量纲一致）"},
            },
        },
        "invoke": {"action": "SET_COLOR"},
    },
    "SET_STATE": {
        "id": "set_light_state",
        "name": "开关灯",
        "description": "切换灯环的照明状态",
        "tags": ["actuator", "light"],
        "params_schema": {
            "type": "object",
            "required": ["state"],
            "properties": {
                "state": {"enum": ["BRIGHT", "DIM", "OFF"]},
            },
        },
        "invoke": {"action": "SET_STATE"},
    },
    "BLINK": {
        "id": "blink_light",
        "name": "闪烁灯光",
        "description": "让灯环闪烁指定次数",
        "tags": ["actuator", "light"],
        "params_schema": {
            "type": "object",
            "required": ["count"],
            "properties": {
                "r":     {"type": "integer", "minimum": 0, "maximum": 255, "description": "红色通道"},
                "g":     {"type": "integer", "minimum": 0, "maximum": 255, "description": "绿色通道"},
                "b":     {"type": "integer", "minimum": 0, "maximum": 255, "description": "蓝色通道"},
                "count": {"type": "integer", "minimum": 1, "maximum": 20, "description": "闪烁次数"},
            },
        },
        "invoke": {"action": "BLINK"},
    },
    "PLAY": {
        "id": "play_sound",
        "name": "播放声音",
        "description": "播放设备预置提示音",
        "tags": ["actuator", "sound"],
        "params_schema": {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string", "enum": ["NOTIFY", "ALERT"]},
            },
        },
        "invoke": {"action": "PLAY"},
    },
}

# ── 声明式状态机表 ────────────────────────────────────────────────
# 旧固件没有 fsm_version；保留其拓扑快照，不能依赖升级后的 edge.ism。
_LED_LABELS = {
    "CMD_OFF": "关灯", "CMD_BRIGHT": "调亮", "CMD_DIM": "调暗",
    "CMD_COLOR": "彩光", "CMD_BLINK": "闪烁",
}

_LED_COMMANDS = {
    "CMD_OFF": ("SET_STATE", {"state": "OFF"}),
    "CMD_BRIGHT": ("SET_STATE", {"state": "BRIGHT"}),
    "CMD_DIM": ("SET_STATE", {"state": "DIM"}),
    "CMD_COLOR": ("SET_COLOR", {"r": 255, "g": 200, "b": 80, "brightness": 180}),
    "CMD_BLINK": ("BLINK", {"r": 255, "g": 255, "b": 255, "count": 3}),
}
_LEGACY_LED_TABLE = {
    ("OFF", "CMD_OFF"): "OFF", ("OFF", "CMD_BRIGHT"): "BRIGHT",
    ("OFF", "CMD_DIM"): "DIM", ("OFF", "CMD_COLOR"): "COLOR",
    ("OFF", "CMD_BLINK"): "BLINK",
    ("BRIGHT", "CMD_OFF"): "OFF", ("BRIGHT", "CMD_DIM"): "DIM",
    ("BRIGHT", "CMD_COLOR"): "COLOR", ("BRIGHT", "CMD_BLINK"): "BLINK",
    ("DIM", "CMD_OFF"): "OFF", ("DIM", "CMD_BRIGHT"): "BRIGHT",
    ("DIM", "CMD_COLOR"): "COLOR", ("DIM", "CMD_BLINK"): "BLINK",
    ("COLOR", "CMD_OFF"): "OFF", ("COLOR", "CMD_BRIGHT"): "BRIGHT",
    ("COLOR", "CMD_DIM"): "DIM", ("COLOR", "CMD_COLOR"): "COLOR",
    ("COLOR", "CMD_BLINK"): "BLINK",
    ("BLINK", "CMD_OFF"): "OFF", ("BLINK", "CMD_BRIGHT"): "BRIGHT",
    ("BLINK", "CMD_DIM"): "DIM", ("BLINK", "CMD_COLOR"): "COLOR",
}
_LED_EDGES = [
    (src, trigger, dst, *_LED_COMMANDS[trigger])
    for (src, trigger), dst in _LEGACY_LED_TABLE.items()
]

FSM_DEFS: dict[str, StateMachine] = {
    "led": {
        "state_field": "ism",
        "states": ["OFF", "DIM", "BRIGHT", "COLOR", "BLINK", "ERROR"],
        "transitions": [
            {"src": s, "dst": d, "trigger": t, "label": _LED_LABELS[t], "action": a, "params": p}
            for (s, t, d, a, p) in _LED_EDGES
        ],
        "initial": "OFF",
        "views": {
            "OFF": {
                "label": "熄灭", "description": "灯环熄灭，等待下一条照明指令。",
                "type": "light_ring", "color": "#505363", "intensity": 0.08,
            },
            "DIM": {
                "label": "柔光", "description": "低亮暖光，适合休息和夜间环境。",
                "type": "light_ring", "color": "#FFD08A", "intensity": 0.34,
            },
            "BRIGHT": {
                "label": "亮光", "description": "高亮度照明，适合工作和阅读。",
                "type": "light_ring", "color": "#FFF4D6", "intensity": 1.0,
            },
            "COLOR": {
                "label": "彩光", "description": "以设定的颜色和亮度营造空间氛围。",
                "type": "light_ring", "color": "#FFB45C", "intensity": 0.78,
                "palette": ["#FF8A5C", "#FFD05C", "#7EE8A2", "#6B8CFF", "#D76BFF"],
            },
            "BLINK": {
                "label": "闪烁", "description": "灯环间歇闪烁，用于提醒或引起注意。",
                "type": "light_ring", "color": "#FFFFFF", "intensity": 0.9, "motion": "pulse",
            },
            "ERROR": {
                "label": "异常", "description": "灯光执行异常，请检查设备连接与供电。",
                "type": "light_ring", "color": "#FF5252", "intensity": 0.65, "motion": "alert",
            },
        },
    },
}

_NEW_LED_TRANSITIONS = [
    ("OFF", "ON", "CMD_ON", "开灯", "SET_STATE", {"state": "ON"}),
    ("ON", "ON", "CMD_ON", "开灯", "SET_STATE", {"state": "ON"}),
    ("BLINKING", "ON", "CMD_ON", "常亮", "SET_STATE", {"state": "ON"}),
    ("OFF", "OFF", "CMD_OFF", "关灯", "SET_STATE", {"state": "OFF"}),
    ("ON", "OFF", "CMD_OFF", "关灯", "SET_STATE", {"state": "OFF"}),
    ("BLINKING", "OFF", "CMD_OFF", "关灯", "SET_STATE", {"state": "OFF"}),
    ("OFF", "BLINKING", "CMD_BLINK", "闪烁", "BLINK", {"count": 3}),
    ("ON", "BLINKING", "CMD_BLINK", "闪烁", "BLINK", {"count": 3}),
    ("BLINKING", "BLINKING", "CMD_BLINK", "再闪烁", "BLINK", {"count": 3}),
    ("BLINKING", "OFF", "BLINK_DONE", "自动熄灭", "", {}),
    ("BLINKING", "ON", "BLINK_DONE", "自动常亮", "", {}),
]

NEW_LED_FSM: StateMachine = {
    "state_field": "ism",
    "states": ["OFF", "ON", "BLINKING"],
    "initial": "OFF",
    "transitions": [
        {"src": src, "dst": dst, "trigger": trigger, "label": label,
         "action": action, "params": params}
        for src, dst, trigger, label, action, params in _NEW_LED_TRANSITIONS
    ],
    "views": {
        "OFF": {"label": "熄灭", "description": "灯环熄灭。",
                "type": "light_ring", "color": "#505363", "intensity": 0.08},
        "ON": {"label": "常亮", "description": "按当前颜色和亮度持续发光。",
               "type": "light_ring", "color": "#FFFFFF", "intensity": 1.0},
        "BLINKING": {"label": "闪烁", "description": "闪烁结束后恢复先前灯光。",
                     "type": "light_ring", "color": "#FFFFFF", "intensity": 0.9,
                     "motion": "pulse"},
    },
}

NEW_SET_STATE_SKILL: SkillDef = {
    **CAPABILITY_SKILLS["SET_STATE"],
    "params_schema": {
        "type": "object", "required": ["state"],
        "properties": {"state": {"enum": ["ON", "OFF"]}},
    },
}

# ── 声明式模式轴表 ────────────────────────────────────────────────
# 灯只有「自主性」一个轴；端点指向现有 /api/esp32/autonomy（不改后端）。
_LED_MODES: list[dict] = [{
    "id": "autonomy",
    "label": "自主性",
    "options": [
        {"value": "manual",   "label": "仅听指令", "description": "只执行用户/编排器命令，不自发改灯"},
        {"value": "reactive", "label": "自动调光", "description": "灯自发根据环境调色温与亮度"},
    ],
    "get": "/api/esp32/autonomy",
    "set": "/api/esp32/autonomy",
    "set_action": "set_mode",
}]

MODE_DEFS: dict[str, list[dict]] = {"led": _LED_MODES}

# ── 声明式快捷指令表 ──────────────────────────────────────────────
# 设备页对话的快捷词，由 card.suggestions 驱动（前端无设备特判）。
# 灯走直达调教入口，故给的是"调教/对话"风格示例而非即时控制命令。
_LED_SUGGESTIONS: list[str] = [
    "以后天黑了就调亮一点",
    "我睡觉时开助眠灯",
    "我工作时开护眼光",
]

SUGGESTION_DEFS: dict[str, list[str]] = {"led": _LED_SUGGESTIONS}

_LED_COLOR_SWATCHES: list[dict] = [
    {"label": "暖白", "r": 255, "g": 226, "b": 190, "brightness": 180},
    {"label": "自然白", "r": 255, "g": 248, "b": 235, "brightness": 180},
    {"label": "冷白", "r": 220, "g": 235, "b": 255, "brightness": 180},
]

_LED_WIDGETS: list[dict] = [{
    "type": "led_color",
    "states": ["ON", "BLINKING"],
    "placement": "controls",
    "order": 10,
    "action": "SET_COLOR",
    "swatches": _LED_COLOR_SWATCHES,
}]

WIDGET_DEFS: dict[str, list[dict]] = {"led": _LED_WIDGETS}


def _copy_widgets(widgets: list[dict]) -> list[dict]:
    copied: list[dict] = []
    for widget in widgets:
        item = dict(widget)
        if "swatches" in item:
            item["swatches"] = [dict(swatch) for swatch in item["swatches"]]
        copied.append(item)
    return copied


def _fsm_key(manifest: dict) -> str:
    """选用哪张 FSM：优先 manifest['fsm'] 提示，否则按 name/unit_id 猜。"""
    hint = manifest.get("fsm")
    if hint in FSM_DEFS:
        return hint
    name = (manifest.get("name", "") + manifest.get("unit_id", "")).lower()
    if any(k in name for k in ("led", "rgb", "ws2812", "ring", "灯")):
        return "led"
    return ""


def build_card_from_manifest(manifest: dict) -> AgentCard:
    """从 ESP32 MQTT manifest 字典组装完整 AgentCard。

    unit_id 为唯一标识（注册表 key、topic、URL 都用它）。
    skills 只映射 CAPABILITY_SKILLS 中已知的 action，未知 action 静默跳过。
    online 固定为 True（收到 manifest 即表示设备在线）。
    state 固定为空 {}（动态状态由 state topic 单独维护）。
    """
    unit_id = manifest.get("unit_id", "")
    name = manifest.get("name", unit_id)
    agent_type = manifest.get("agent_type", "sensor")
    description = manifest.get("description") or f"{name} 智能体"

    transport: Transport = {
        "kind": "mqtt",
        "task_topic": f"ssm/task/{unit_id}/{{task_id}}",  # 用 unit_id，保证 ESP32 能收到
    }

    capabilities: list[dict] = manifest.get("capabilities", [])
    capability_actions = {cap.get("action", "") for cap in capabilities}
    skills: list[SkillDef] = []
    for cap in capabilities:
        action = cap.get("action", "")
        skill = (NEW_SET_STATE_SKILL if action == "SET_STATE" and
                 _fsm_key(manifest) == "led" and manifest.get("fsm_version") == 2
                 else CAPABILITY_SKILLS.get(action))
        if skill:
            skills.append(skill)

    card = AgentCard(
        card_schema_version=1,
        version=str(manifest.get("version") or manifest.get("firmware_version") or "unknown"),
        unit_id=unit_id,
        device_id=manifest.get("parent_id") or unit_id,
        name=name,
        description=description,
        agent_type=agent_type,
        online=True,
        transport=transport,
        skills=skills,
        state={},
    )
    card["parent_id"] = manifest.get("parent_id", "")
    card["tags"] = list(manifest.get("resource_tags") or manifest.get("tags") or [])
    fsm_key = _fsm_key(manifest)
    if fsm_key:
        if fsm_key == "led" and agent_type == "actuator":
            card["services"] = {"chat_endpoint": f"/api/esp32/{unit_id}/chat"}
        card["state_machine"] = (NEW_LED_FSM if fsm_key == "led" and
                                 manifest.get("fsm_version") == 2 else FSM_DEFS[fsm_key])
        modes = MODE_DEFS.get(fsm_key)
        if modes:
            card["modes"] = modes
        suggestions = SUGGESTION_DEFS.get(fsm_key)
        if suggestions:
            card["suggestions"] = suggestions
        widgets = WIDGET_DEFS.get(fsm_key)
        if widgets and "SET_COLOR" in capability_actions:
            card["widgets"] = _copy_widgets(widgets)
            if fsm_key == "led" and manifest.get("fsm_version") != 2:
                card["widgets"][0]["states"] = ["BRIGHT", "DIM", "COLOR"]
    return normalize_card(card)


def parse_card(payload: dict) -> AgentCard:
    """验证并返回自描述 Agent Card（Go2 等 HTTP 设备使用）。

    state_machine 字段若存在则原样透传（Go2 等设备自带拓扑描述）。
    """
    return normalize_card(payload)
