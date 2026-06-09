"""devices.py — 设备列表与 A2A Agent Card 端点。"""
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()

_DEVICES_FILE = Path(__file__).parent.parent / "orchestrator" / "devices.json"


def _load_devices() -> dict:
    try:
        return json.loads(_DEVICES_FILE.read_text()) if _DEVICES_FILE.exists() else {}
    except Exception:
        return {}


def _find_device_by_slug(slug: str) -> dict | None:
    for device in _load_devices().values():
        if device.get("slug") == slug:
            return device
    return None


# ── go2 技能表 ────────────────────────────────────────────────────

_GO2_STATIC_SKILLS = [
    {
        "id":          "go2_chat",
        "name":        "自然语言交互",
        "description": "用自然语言向机器狗下达指令或提问，支持复合任务分解",
        "tags":        ["chat", "nlp"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
    {
        "id":          "go2_observe",
        "name":        "视觉感知",
        "description": "用摄像头描述当前场景，或回答关于画面内容的问题",
        "tags":        ["vision"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
    {
        "id":          "go2_navigate",
        "name":        "导航到命名地点",
        "description": "导航到已保存的命名地点，支持模糊描述",
        "tags":        ["navigation"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
        "inputSchema": {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string", "description": "目标地点名称"}},
        },
    },
    {
        "id":          "go2_patrol",
        "name":        "巡逻",
        "description": "在多个命名地点之间循环巡逻",
        "tags":        ["navigation"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
        "inputSchema": {
            "type": "object", "required": ["stops"],
            "properties": {"stops": {"type": "array", "items": {"type": "string"}}},
        },
    },
    {
        "id":          "go2_tag_location",
        "name":        "保存当前位置",
        "description": "将机器狗当前所在位置保存为命名地点，供后续导航使用",
        "tags":        ["navigation", "memory"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
        "inputSchema": {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string", "description": "地点名称"}},
        },
    },
    {
        "id":          "go2_add_rule",
        "name":        "添加视觉触发规则",
        "description": "检测到指定视觉关键词时自动触发预定义动作",
        "tags":        ["rules", "vision"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
        "inputSchema": {
            "type": "object", "required": ["trigger", "action"],
            "properties": {
                "trigger": {"type": "string", "description": "视觉触发关键词，如「人」「猫」"},
                "action":  {"type": "string", "description": "触发后执行的动作名"},
                "cooldown_s": {"type": "integer", "default": 30},
            },
        },
    },
]

_SPORT_META = {
    "StandUp":   ("起立", "从趴下状态站起来"),
    "StandDown": ("趴下", "从站立状态趴下"),
    "Hello":     ("打招呼", "挥手打招呼"),
    "Stretch":   ("伸展", "做伸展动作"),
    "Dance1":    ("舞蹈一", "表演舞蹈动作一"),
    "Dance2":    ("舞蹈二", "表演舞蹈动作二"),
}


def _go2_dynamic_skills(available_actions: list[str]) -> list[dict]:
    """根据 FSM available_actions 动态生成运动技能。"""
    skills = []
    sport_cmds = [a for a in available_actions if a in _SPORT_META]
    if sport_cmds:
        skills.append({
            "id":          "go2_sport",
            "name":        "预定义动作",
            "description": "执行预定义肢体动作：" + "、".join(
                f"{_SPORT_META[c][0]}({c})" for c in sport_cmds
            ),
            "tags":        ["motion"],
            "inputModes":  ["text"],
            "outputModes": ["text"],
            "inputSchema": {
                "type": "object", "required": ["cmd"],
                "properties": {"cmd": {"type": "string", "enum": sport_cmds}},
            },
        })
    if "Move" in available_actions:
        skills.append({
            "id":          "go2_move",
            "name":        "方向移动",
            "description": "向指定方向持续移动，支持 forward/backward/left/right/turn_left/turn_right",
            "tags":        ["motion", "navigation"],
            "inputModes":  ["text"],
            "outputModes": ["text"],
            "inputSchema": {
                "type": "object", "required": ["direction"],
                "properties": {
                    "direction": {"type": "string",
                                  "enum": ["forward", "backward", "left", "right",
                                           "turn_left", "turn_right"]},
                    "speed":    {"type": "number", "default": 0.3},
                    "duration": {"type": "number", "default": 1.0},
                },
            },
        })
    return skills


# ── ESP32 技能表 ──────────────────────────────────────────────────

_ESP32_CAPABILITY_SKILLS: dict[str, dict] = {
    "led": {
        "id":          "esp32_led",
        "name":        "LED 控制",
        "description": "设置 LED 颜色、亮度或动画效果",
        "tags":        ["actuator", "light"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
    "buzzer": {
        "id":          "esp32_buzzer",
        "name":        "蜂鸣器",
        "description": "触发蜂鸣器发出提示音",
        "tags":        ["actuator", "audio"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
    "light_sensor": {
        "id":          "esp32_light",
        "name":        "光线传感",
        "description": "读取当前环境光照强度",
        "tags":        ["sensor"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
    "sound_sensor": {
        "id":          "esp32_sound",
        "name":        "声音传感",
        "description": "检测环境声音强度",
        "tags":        ["sensor"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
    "ir_sensor": {
        "id":          "esp32_ir",
        "name":        "红外传感",
        "description": "检测红外信号或障碍物",
        "tags":        ["sensor"],
        "inputModes":  ["text"],
        "outputModes": ["text"],
    },
}


def _esp32_skills(capabilities: list[str]) -> list[dict]:
    return [_ESP32_CAPABILITY_SKILLS[c] for c in capabilities if c in _ESP32_CAPABILITY_SKILLS]


# ── 路由 ─────────────────────────────────────────────────────────

@router.get("/api/devices")
def list_devices():
    """列出所有在线设备。"""
    devices = [d for d in _load_devices().values() if d.get("slug")]
    return [
        {
            "unit_id":    d.get("unit_id"),
            "name":       d.get("name"),
            "slug":       d.get("slug"),
            "agent_type": d.get("agent_type"),
            "online":     True,
        }
        for d in devices
    ]


@router.get("/api/devices/{slug}/agent")
def device_agent_card(slug: str):
    """A2A Agent Card —— 机器可读的智能体能力描述，供编排器动态发现和调用。"""
    base_url = os.getenv("PUBLIC_BASE_URL", "")

    if slug == "go2":
        from cloud.go2.connection import go2 as _go2
        return {
            # A2A 标准字段
            "name":               "Go2 机器狗",
            "description":        "宇树 Go2 四足机器人，支持运动控制、自主导航、视觉感知和自然语言交互",
            "url":                f"{base_url}/api/go2/chat",
            "version":            "1.0.0",
            "capabilities": {
                "streaming":             True,
                "pushNotifications":     False,
                "stateTransitionHistory": False,
            },
            "defaultInputModes":  ["text"],
            "defaultOutputModes": ["text"],
            "skills":             _GO2_STATIC_SKILLS + _go2_dynamic_skills(_go2.available_actions),
            # 扩展字段
            "slug":               "go2",
            "agent_type":         "robot",
            "online":             _go2.is_connected,
            "state":              _go2.fsm_state,
        }

    device = _find_device_by_slug(slug)
    if not device:
        raise HTTPException(status_code=404, detail=f"设备 '{slug}' 不存在或未上线")

    return {
        # A2A 标准字段
        "name":               device.get("name", slug),
        "description":        f"ESP32 边缘智能体，具备 {', '.join(device.get('capabilities', []))} 能力",
        "url":                f"{base_url}/api/nlu",
        "version":            "1.0.0",
        "capabilities": {
            "streaming":             False,
            "pushNotifications":     False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes":  ["text"],
        "defaultOutputModes": ["text"],
        "skills":             _esp32_skills(device.get("capabilities", [])),
        # 扩展字段
        "slug":               slug,
        "unit_id":            device.get("unit_id", ""),
        "agent_type":         device.get("agent_type", ""),
        "online":             True,
        "ts":                 device.get("ts"),
    }
