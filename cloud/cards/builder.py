"""cloud.cards.builder — 将 ESP32 manifest 或自描述 card payload 组装成 AgentCard。

构建逻辑：
- build_card_from_manifest：从 MQTT manifest 字典构建（ESP32 设备）。
- parse_card：从自描述 card payload 构建（Go2 等 HTTP 设备，Task 2 接入）。
"""

from __future__ import annotations

from cloud.cards.schema import AgentCard, SkillDef, SkillInvoke, StateMachine, Transport


def expand_mode_skills(modes: list[dict] | None) -> list[SkillDef]:
    """把每个 ModeAxis 展开成一条 set_{axis_id} skill，供编排器统一调用。

    params_schema.value 的 enum 即该轴 options 的 value 集合；
    invoke.action 取 set（http 端点）或 set_topic（mqtt topic）。
    """
    skills: list[SkillDef] = []
    for axis in modes or []:
        axis_id = axis.get("id", "")
        if not axis_id:
            continue
        values = [o["value"] for o in axis.get("options", [])]
        skills.append({
            "id": f"set_{axis_id}",
            "name": f"切换{axis.get('label', axis_id)}",
            "tags": ["mode"],
            "params_schema": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"enum": values}},
            },
            "invoke": {"action": axis.get("set") or axis.get("set_topic") or ""},
        })
    return skills


# ── 声明式技能映射表 ──────────────────────────────────────────────
# 将 ESP32 manifest.capabilities 中的每条 action 映射到完整 SkillDef 结构。
# 新增传感器 action 只需在此表添加一条，build_card_from_manifest 无需修改。
CAPABILITY_SKILLS: dict[str, SkillDef] = {
    "SET_COLOR": {
        "id": "set_light_color",
        "name": "设置灯光颜色",
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
        "tags": ["actuator", "light"],
        "params_schema": {
            "type": "object",
            "required": ["count"],
            "properties": {
                "r":     {"type": "integer", "minimum": 0, "maximum": 255, "description": "红色通道"},
                "g":     {"type": "integer", "minimum": 0, "maximum": 255, "description": "绿色通道"},
                "b":     {"type": "integer", "minimum": 0, "maximum": 255, "description": "蓝色通道"},
                "count": {"type": "integer", "minimum": 1, "description": "闪烁次数"},
            },
        },
        "invoke": {"action": "BLINK"},
    },
    "PLAY": {
        "id": "play_sound",
        "name": "播放声音",
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
# 镜像 edge/ism.py 的 LED_TABLE/SENSOR_TABLE（设备侧是真行为，此表给 UI 画图）。
# 新增状态：先改 ism.py（真行为），再在此表加对应转移。
_LED_LABELS = {
    "CMD_OFF": "关灯", "CMD_BRIGHT": "亮", "CMD_DIM": "调暗",
    "CMD_COLOR": "彩色", "CMD_BLINK": "闪烁",
}

# (src, trigger, dst, action, params) — action/params 供前端直接发 MQTT task
_LED_EDGES = [
    ("OFF",    "CMD_BRIGHT", "BRIGHT", "SET_STATE", {"state": "BRIGHT"}),
    ("OFF",    "CMD_DIM",    "DIM",    "SET_STATE", {"state": "DIM"}),
    ("OFF",    "CMD_COLOR",  "COLOR",  "SET_COLOR", {"r": 255, "g": 200, "b": 80,  "brightness": 180}),
    ("OFF",    "CMD_BLINK",  "BLINK",  "BLINK",     {"r": 255, "g": 255, "b": 255, "count": 3}),
    ("BRIGHT", "CMD_OFF",    "OFF",    "SET_STATE", {"state": "OFF"}),
    ("BRIGHT", "CMD_DIM",    "DIM",    "SET_STATE", {"state": "DIM"}),
    ("BRIGHT", "CMD_COLOR",  "COLOR",  "SET_COLOR", {"r": 255, "g": 200, "b": 80,  "brightness": 180}),
    ("BRIGHT", "CMD_BLINK",  "BLINK",  "BLINK",     {"r": 255, "g": 255, "b": 255, "count": 3}),
    ("DIM",    "CMD_OFF",    "OFF",    "SET_STATE", {"state": "OFF"}),
    ("DIM",    "CMD_BRIGHT", "BRIGHT", "SET_STATE", {"state": "BRIGHT"}),
    ("DIM",    "CMD_COLOR",  "COLOR",  "SET_COLOR", {"r": 255, "g": 200, "b": 80,  "brightness": 180}),
    ("DIM",    "CMD_BLINK",  "BLINK",  "BLINK",     {"r": 255, "g": 255, "b": 255, "count": 3}),
    ("COLOR",  "CMD_OFF",    "OFF",    "SET_STATE", {"state": "OFF"}),
    ("COLOR",  "CMD_BRIGHT", "BRIGHT", "SET_STATE", {"state": "BRIGHT"}),
    ("COLOR",  "CMD_DIM",    "DIM",    "SET_STATE", {"state": "DIM"}),
    ("COLOR",  "CMD_BLINK",  "BLINK",  "BLINK",     {"r": 255, "g": 255, "b": 255, "count": 3}),
    ("BLINK",  "CMD_OFF",    "OFF",    "SET_STATE", {"state": "OFF"}),
    ("BLINK",  "CMD_BRIGHT", "BRIGHT", "SET_STATE", {"state": "BRIGHT"}),
    ("BLINK",  "CMD_DIM",    "DIM",    "SET_STATE", {"state": "DIM"}),
    ("BLINK",  "CMD_COLOR",  "COLOR",  "SET_COLOR", {"r": 255, "g": 200, "b": 80,  "brightness": 180}),
]

FSM_DEFS: dict[str, StateMachine] = {
    "led": {
        "states": ["OFF", "DIM", "BRIGHT", "COLOR", "BLINK", "ERROR"],
        "transitions": [
            {"src": s, "dst": d, "trigger": t, "label": _LED_LABELS[t], "action": a, "params": p}
            for (s, t, d, a, p) in _LED_EDGES
        ],
        "initial": "OFF",
    },
}

# ── 声明式模式轴表 ────────────────────────────────────────────────
# 灯只有「自主性」一个轴；端点指向现有 /api/esp32/autonomy（不改后端）。
_LED_MODES: list[dict] = [{
    "id": "autonomy",
    "label": "自主性",
    "options": [
        {"value": "reactive", "label": "自动调光", "description": "灯自发根据环境调色温与亮度"},
        {"value": "manual",   "label": "仅听指令", "description": "只执行用户/编排器命令，不自发改灯"},
    ],
    "get": "/api/esp32/autonomy",
    "set": "/api/esp32/autonomy",
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
    description = manifest.get("description", "")

    transport: Transport = {
        "kind": "mqtt",
        "task_topic": f"ssm/task/{unit_id}/{{task_id}}",  # 用 unit_id，保证 ESP32 能收到
        "chat_endpoint": f"/api/esp32/{unit_id}/chat",    # ESP32 设备对话端点（所有 esp32 unit；执行器详情页使用，单一 ESP32 智能体处理）
    }

    capabilities: list[dict] = manifest.get("capabilities", [])
    skills: list[SkillDef] = []
    for cap in capabilities:
        action = cap.get("action", "")
        skill = CAPABILITY_SKILLS.get(action)
        if skill:
            skills.append(skill)

    card = AgentCard(
        unit_id=unit_id,
        name=name,
        description=description,
        agent_type=agent_type,
        online=True,
        transport=transport,
        skills=skills,
        state={},
    )
    card["parent_id"] = manifest.get("parent_id", "")
    fsm_key = _fsm_key(manifest)
    if fsm_key:
        card["state_machine"] = FSM_DEFS[fsm_key]
        modes = MODE_DEFS.get(fsm_key)
        if modes:
            card["modes"] = modes
            existing = {s["id"] for s in card["skills"]}
            card["skills"].extend(
                s for s in expand_mode_skills(modes) if s["id"] not in existing
            )
        suggestions = SUGGESTION_DEFS.get(fsm_key)
        if suggestions:
            card["suggestions"] = suggestions
    return card


def parse_card(payload: dict) -> AgentCard:
    """验证并返回自描述 Agent Card（Go2 等 HTTP 设备使用）。

    unit_id 缺省回退 payload['slug']，兼容烧录/重启前的旧 retained card。
    state_machine 字段若存在则原样透传（Go2 等设备自带拓扑描述）。
    """
    card = AgentCard(
        unit_id=payload.get("unit_id") or payload.get("slug", ""),
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        agent_type=payload.get("agent_type", ""),
        online=payload.get("online", False),
        transport=payload.get("transport", {"kind": "http"}),
        skills=payload.get("skills", []),
        state=payload.get("state", {}),
    )
    if "state_machine" in payload:
        card["state_machine"] = payload["state_machine"]
    for key in ("modes", "telemetry", "widgets", "suggestions"):
        if key in payload:
            card[key] = payload[key]
    if payload.get("modes"):
        existing = {s["id"] for s in card["skills"]}
        card["skills"] = list(card["skills"]) + [
            s for s in expand_mode_skills(payload["modes"]) if s["id"] not in existing
        ]
    return card
