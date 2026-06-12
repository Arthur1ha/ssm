"""cloud.cards.builder — 将 ESP32 manifest 或自描述 card payload 组装成 AgentCard。

构建逻辑：
- build_card_from_manifest：从 MQTT manifest 字典构建（ESP32 设备）。
- parse_card：从自描述 card payload 构建（Go2 等 HTTP 设备，Task 2 接入）。
"""

from __future__ import annotations

from cloud.cards.schema import AgentCard, SkillDef, SkillInvoke, Transport

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
                "brightness": {"type": "integer", "minimum": 0, "maximum": 100,  "description": "亮度百分比"},
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


def build_card_from_manifest(manifest: dict) -> AgentCard:
    """从 ESP32 MQTT manifest 字典组装完整 AgentCard。

    slug 优先取 manifest['slug']，缺省用 unit_id。
    skills 只映射 CAPABILITY_SKILLS 中已知的 action，未知 action 静默跳过。
    online 固定为 True（收到 manifest 即表示设备在线）。
    state 固定为空 {}（动态状态由 state topic 单独维护）。
    """
    unit_id = manifest.get("unit_id", "")
    slug = manifest.get("slug") or unit_id
    name = manifest.get("name", unit_id)
    agent_type = manifest.get("agent_type", "sensor")
    description = manifest.get("description", "")

    transport: Transport = {
        "kind": "mqtt",
        "task_topic": f"ssm/task/{unit_id}/{{task_id}}",  # 用 unit_id，保证 ESP32 能收到
    }

    capabilities: list[dict] = manifest.get("capabilities", [])
    skills: list[SkillDef] = []
    for cap in capabilities:
        action = cap.get("action", "")
        skill = CAPABILITY_SKILLS.get(action)
        if skill:
            skills.append(skill)

    return AgentCard(
        slug=slug,
        unit_id=unit_id,
        name=name,
        description=description,
        agent_type=agent_type,
        online=True,
        transport=transport,
        skills=skills,
        state={},
    )


def parse_card(payload: dict) -> AgentCard:
    """验证并返回自描述 Agent Card（Go2 等 HTTP 设备使用）。

    当前为 pass-through 实现，Task 2 接入 Go2 后在此添加校验逻辑。
    """
    return AgentCard(
        slug=payload.get("slug", ""),
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        agent_type=payload.get("agent_type", ""),
        online=payload.get("online", False),
        transport=payload.get("transport", {"kind": "http"}),
        skills=payload.get("skills", []),
        state=payload.get("state", {}),
    )
