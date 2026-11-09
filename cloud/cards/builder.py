"""cloud.cards.builder — 将 ESP32 manifest 或自描述 card payload 组装成 AgentCard。

只构建当前 ESP32 manifest 的静态能力声明；自描述卡直接通过统一校验。
"""

from __future__ import annotations

from copy import deepcopy

from cloud.cards.schema import AgentCard, StateRegion
from cloud.cards.validation import normalize_card
from cloud.esp32.tool_registry import LAMP_SKILLS, lamp_skill
from edge.ism import LED_TABLE, LED_STATES, BLINK_DONE_TARGETS
from edge.lamp_contract import BLINK_DEFAULT, FSM_VERSION


# Lamp topology is projected directly from the current edge contract.
_LED_COMMANDS = {
    "CMD_ON": ("开灯", "SET_STATE", {"state": "ON"}),
    "CMD_OFF": ("关灯", "SET_STATE", {"state": "OFF"}),
    "CMD_COLOR": ("调色", "SET_COLOR", {"r": 255, "g": 200, "b": 80, "brightness": 180}),
    "CMD_BLINK": ("闪烁", "BLINK", {"count": BLINK_DEFAULT}),
}


def _current_led_transitions():
    transitions = []
    for (src, trigger), dst in LED_TABLE.items():
        label, action, params = _LED_COMMANDS[trigger]
        transitions.append({"src": src, "dst": dst, "trigger": trigger,
                            "label": label, "action": action, "params": deepcopy(params),
                            "trigger_condition": f"收到{label}指令",
                            "graph_hidden": trigger == "CMD_COLOR",
                            **({"training_events": ["sensor.sound_detected"]} if action == "BLINK" else {}),
                            **({"training_fixed_params": ["state"]} if action == "SET_STATE" else {})})
    for target in BLINK_DONE_TARGETS:
        transitions.append({"src": "BLINKING", "dst": target, "trigger": "BLINK_DONE",
                            "label": "闪烁结束",
                            "trigger_condition": "指定次数完成，且闪烁前为" + ("熄灭" if target == "OFF" else "常亮"),
                            "action": "", "params": {}, "graph_hidden": False})
    return transitions


LED_REGION: StateRegion = {
    "state_field": "ism",
    "states": list(LED_STATES),
    "transitions": _current_led_transitions(),
    "views": {
        "OFF": {"label": "熄灭", "description": "灯环熄灭。",
                "type": "light_ring"},
        "ON": {"label": "常亮", "description": "按当前颜色和亮度持续发光。",
               "type": "light_ring"},
        "BLINKING": {"label": "闪烁", "description": "持续闪烁，直到切换为常亮或熄灭；仅指定次数时自动结束。",
                     "type": "light_ring"},
    },
}

# ── 声明式模式轴表 ────────────────────────────────────────────────
# 灯只有「自主性」一个轴；端点指向现有 /api/esp32/autonomy（不改后端）。
_LED_MODES: list[dict] = [{
    "id": "autonomy",
    "label": "自主性",
    "state_field": "autonomy_mode",
    "options": [
        {"value": "manual",   "label": "仅听指令", "description": "只执行用户/编排器命令，不自发改灯"},
        {"value": "reactive", "label": "自动调光", "description": "灯自发根据环境调色温与亮度"},
    ],
    "get": "/api/esp32/autonomy",
    "set_action": "set_mode",
}]


# ── 声明式快捷指令表 ──────────────────────────────────────────────
# 设备页对话的快捷词，由 card.suggestions 驱动（前端无设备特判）。
# 灯页展示调光、色温与闪烁，均由灯智能体独立处理。
_LED_SUGGESTIONS: list[str] = [
    "把灯调成适合阅读的亮度和色温",
    "把灯调成暖白色，亮度降低一些",
    "闪烁三次提醒我",
]


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


def build_card_from_manifest(manifest: dict) -> AgentCard:
    """Build a current static Card from the edge capability declaration."""
    unit_id = manifest.get("unit_id", "")
    capabilities = manifest.get("capabilities", [])
    actions = {cap.get("action") for cap in capabilities}
    is_lamp = bool(actions.intersection(LAMP_SKILLS))
    if is_lamp and manifest.get("fsm_version") != FSM_VERSION:
        raise ValueError(f"unsupported lamp fsm_version={manifest.get('fsm_version')!r} for {unit_id}; "
                         f"expected {FSM_VERSION}, update the ESP32 scripts")
    skills = []
    for cap in capabilities:
        action = cap.get("action")
        if action not in LAMP_SKILLS:
            continue
        skill = lamp_skill(action)
        if cap.get("params_schema") is not None and cap["params_schema"] != skill["params_schema"]:
            raise ValueError("lamp manifest parameter contract does not match fsm_version")
        skills.append(skill)
    card = {
        "card_schema_version": 1,
        "version": manifest["firmware_ver"],
        "unit_id": unit_id,
        "device_id": manifest.get("parent_id") or unit_id,
        "name": manifest["name"],
        "description": manifest.get("description") or f"{manifest['name']} 智能体",
        "agent_type": manifest["agent_type"],
        "transport": {"kind": "mqtt", "task_topic": f"ssm/task/{unit_id}/{{task_id}}"},
        "skills": skills,
        "tags": list(manifest.get("tags", [])),
    }
    if is_lamp:
        from cloud.esp32.card import adjust_lighting_skill
        if "SET_COLOR" in actions:
            skills.append(adjust_lighting_skill(unit_id))
            card["widgets"] = deepcopy(_LED_WIDGETS)
        card["services"] = {"chat_endpoint": f"/api/esp32/{unit_id}/chat"}
        card["observations"] = [
            {"id": "sensor.sound_detected", "fields": {"detected": "boolean"}},
            {"id": "sensor.light_changed", "fields": {"level": "string"}},
        ]
        card["state_machine"] = {"primary_region": "device", "regions": [
            {"id": "device", "label": "照明", **deepcopy(LED_REGION)},
        ]}
        card["modes"] = deepcopy(_LED_MODES)
        card["suggestions"] = list(_LED_SUGGESTIONS)
    from cloud.agents.common.training.catalog import declare_transition_ids
    return declare_transition_ids(normalize_card(card))
