"""Go2 Agent Card and retained MQTT control-plane lifecycle."""

import json
import logging
import time
from copy import deepcopy
from cloud.cards.validation import normalize_card

from cloud.go2.connection import go2

GO2_CARD_TOPIC = "ssm/agents/go2/card"
GO2_STATUS_TOPIC = "ssm/agents/go2/status"
GO2_STATE_TOPIC = "ssm/agents/go2/state"
_mqtt_client = None
_last_status_online: bool | None = None

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
        "description": "执行机器狗预定义动作",
        "tags": ["motion", "robot"],
        "invoke": {"action": "SPORT"},
    },
    "go2_navigate": {
        "tool_name": "go2_navigate_to",
        "name": "导航到命名地点",
        "description": "仅在当前会话拥有可信定位与已知安全路线时导航；条件不足会拒绝移动",
        "tags": ["navigation", "robot"],
        "invoke": {"action": "NAVIGATE"},
    },
    "go2_recognize_person": {
        "tool_name": "go2_recognize_person",
        "name": "识别面前的人",
        "description": "识别摄像头视野中的人物",
        "tags": ["vision", "identity", "robot"],
        "invoke": {"action": "RECOGNIZE_PERSON"},
    },
    "go2_move": {
        "tool_name": "go2_move",
        "name": "短距离定时移动",
        "description": "按方向与时长移动一段距离",
        "tags": ["motion", "robot"],
        "invoke": {"action": "MOVE"},
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
        result["params_schema"] = {
            "type": "object", "required": ["message"],
            "properties": {"message": {"type": "string"}},
        }
    else:
        from cloud.go2.agentcore.tools.registry import get_tool_spec
        spec = get_tool_spec(tool_name)
        if spec is None:
            raise RuntimeError(f"Card skill {skill_id} references missing tool {tool_name}")
        result["params_schema"] = deepcopy(spec.params_schema)
    return result


def card_skills() -> list[dict]:
    return [
        {key: value for key, value in get_card_skill(skill_id).items() if key != "tool_name"}
        for skill_id in CARD_SKILLS
    ]


def init_mqtt(client) -> None:
    global _mqtt_client
    _mqtt_client = client
    go2._peer_state_callback = _on_go2_state_change


def _on_go2_state_change() -> None:
    _publish_go2_state()


def _publish_go2_state() -> None:
    """Publish a bounded runtime snapshot, separate from the static card."""
    if _mqtt_client is None:
        return
    try:
        state = {
            "unit_id": "go2",
            "connected": bool(go2.is_connected),
            "fsm_state": go2.fsm_state,
            "ts": time.time(),
        }
        _mqtt_client.publish(GO2_STATE_TOPIC, json.dumps(state), retain=True, qos=1)
    except Exception as exc:
        logging.warning("[Go2] 发布 state 失败: %s", exc)


_GO2_FSM_LABELS = {
    "StandUp": "起立", "StandDown": "趴下", "EnterMoveControl": "进入遥控", "Move": "移动", "StopMove": "退出遥控",
    "Hello": "挥手", "Stretch": "伸展", "Dance1": "舞蹈1", "Dance2": "舞蹈2",
}


def _go2_state_machine() -> dict:
    """依据 connection/fsm.py 的 _FSM_NEXT 生成静态状态机拓扑（单一真相）。

    延迟导入以避免在测试 stub 环境中与 cloud.go2.connection 补丁冲突。
    """
    from cloud.go2.connection.fsm import _FSM_AVAILABLE, _FSM_NEXT, _TRANSIENT_STATES  # noqa: PLC0415
    transitions = []
    for src, edges in _FSM_NEXT.items():
        for trigger, dst in edges.items():
            if trigger == "Move":
                continue  # 有速度的 Move 属于数据面；图上只显示控制状态入口。
            transitions.append({
                "src": src, "dst": dst, "trigger": trigger,
                "label": "停止" if src in _TRANSIENT_STATES else _GO2_FSM_LABELS.get(trigger, trigger),
                "action": trigger,
                "params": {},
            })
    for src in _FSM_AVAILABLE:
        if src in {"offline", "connecting"}:
            continue
        transitions.append({
            "src": src, "dst": "lying" if src == "lying" else "standing",
            "trigger": "EMERGENCY_STOP", "label": "紧急制停",
            "action": "STOP", "params": {}, "safety": True,
        })
    return {
        "state_field": "fsm_state",
        "connection_field": "connected",
        "states": [s for s in _FSM_AVAILABLE if s not in {"offline", "connecting"}],
        "transitions": transitions,
        "initial": "offline",
        "layout": {
            "standing": {"x": 0.50, "y": 0.48},
            "lying": {"x": 0.50, "y": 0.14},
            "greeting": {"x": 0.16, "y": 0.28},
            "stretching": {"x": 0.84, "y": 0.28},
            "dancing1": {"x": 0.16, "y": 0.68},
            "dancing2": {"x": 0.84, "y": 0.68},
            "moving": {"x": 0.50, "y": 0.82},
        },
        "views": {
            "offline": {"label": "未连接", "description": "机器狗尚未建立控制连接。", "type": "robot_pose", "pose": "offline"},
            "connecting": {"label": "正在连接", "description": "正在与机器狗建立实时控制通道。", "type": "robot_pose", "pose": "offline"},
            "lying": {"label": "趴卧", "description": "身体降低并保持稳定，等待起立指令。", "type": "robot_pose", "pose": "lying"},
            "standing": {"label": "站立", "description": "四足稳定站立，可以移动或执行动作。", "type": "robot_pose", "pose": "standing"},
            "moving": {"label": "遥控", "description": "可用摇杆控制行走；松开摇杆会停下，退出遥控后回到站立。", "type": "robot_pose", "pose": "moving"},
            "greeting": {"label": "挥手", "description": "抬起前腿执行问候动作。", "type": "robot_pose", "pose": "greeting"},
            "stretching": {"label": "伸展", "description": "降低身体前部并完成伸展动作。", "type": "robot_pose", "pose": "stretching"},
            "dancing1": {"label": "舞蹈1", "description": "正在执行第一组节奏动作。", "type": "robot_pose", "pose": "dance-left"},
            "dancing2": {"label": "舞蹈2", "description": "正在执行第二组节奏动作。", "type": "robot_pose", "pose": "dance-right"},
        },
    }


def _build_go2_card() -> dict:
    """构建 Go2 Agent Card dict（静态能力描述，符合 AgentCard schema）。

    card 只含能力，不含 volatile state；online/status 表示真实 WebRTC 连接状态，
    FSM/动作等动态状态经 HTTP /api/go2/* 实时获取。
    """
    card = {
        "card_schema_version": 1,
        "version": "1",
        "unit_id": "go2",
        "device_id": "go2",
        "name": "Go2 机器狗",
        "description": "四足机器人，支持运动控制、导航和视觉感知",
        "agent_type": "robot",
        "online": False,
        "transport": {
            "kind": "http",
            "endpoint": "/api/go2/chat",
            "command_endpoint": "/api/go2/commands",
            "state_stream": "/api/go2/connection/stream",
        },
        "skills": card_skills(),
        # 动态状态不进 card（card=静态能力）；FSM/动作经 HTTP /api/go2/* 实时获取
        "state": {},
        "state_machine": _go2_state_machine(),
        "modes": [{
            "id": "autonomy",
            "label": "自主性",
            "options": [
                {"value": "manual",       "label": "完全遥控", "description": "由用户/编排器遥控决定行动"},
                {"value": "reactive",     "label": "自主反应", "description": "机器狗根据视觉输入自主做即时反应"},
                {"value": "free_explore", "label": "自由探索", "description": "需要可信定位和已知可通行地图；当前地图不足时拒绝移动"},
            ],
            "get": "/api/go2/autonomy",
            "set": "/api/go2/autonomy",
            "set_action": "set_mode",
        }],
        "telemetry": [
            {"key": "fsm_state",   "label": "动作状态"},
            {"key": "body_height", "label": "身高", "unit": "m"},
            {"key": "velocity",    "label": "速度", "unit": "m/s"},
        ],
        "widgets": [
            {
                "type": "connection",
                "states": [],
                "placement": "controls",
                "order": 0,
                "endpoint": "/api/go2/connection",
                "status_endpoint": "/api/go2/connection",
                "connect_action": "connect",
                "disconnect_action": "disconnect",
                "auto_connect": True,
                "visible": False,
            },
            {"type": "video", "states": ["lying", "standing", "moving", "greeting", "stretching", "dancing1", "dancing2"], "placement": "media", "order": 10, "endpoint": "/api/go2/video", "requires_connection": True},
            {"type": "map", "states": [], "placement": "media", "order": 15, "endpoint": "/api/go2/map.png", "requires_connection": True},
            {"type": "joystick", "states": ["moving"], "placement": "controls", "order": 20, "action": "velocity"},
        ],
    }
    return normalize_card(card)


def _publish_go2_card() -> None:
    """向 MQTT broker 发布 Go2 card（retained=True，qos=1）。

    若 MQTT 客户端尚未注入则静默失败，不影响连接流程。
    """
    if _mqtt_client is None:
        logging.warning("[Go2] MQTT 客户端未注入，跳过 card 发布")
        return
    try:
        card = _build_go2_card()
        _mqtt_client.publish(GO2_CARD_TOPIC, json.dumps(card, ensure_ascii=False), retain=True, qos=1)
        logging.info("[Go2] 已发布 retained card 到 %s", GO2_CARD_TOPIC)
    except Exception as exc:
        logging.error("[Go2] 发布 card 失败: %s", exc)


def _publish_go2_status(online: bool | None = None, *, force: bool = True) -> None:
    """发布控制面服务在线状态；WebRTC 连接由 state.connected 表示。"""
    if _mqtt_client is None:
        return
    try:
        global _last_status_online
        if online is None:
            online = True
        if not force and online == _last_status_online:
            return
        _mqtt_client.publish(GO2_STATUS_TOPIC, "online" if online else "offline", retain=True, qos=1)
        _last_status_online = online
        logging.info("[Go2] status → %s", "online" if online else "offline")
    except Exception as exc:
        logging.error("[Go2] 发布 status 失败: %s", exc)


def _clear_go2_card() -> None:
    """清除 broker 上的 Go2 retained card（发布空 payload，qos=1）。

    若 MQTT 客户端尚未注入则静默失败，不影响断开流程。
    """
    if _mqtt_client is None:
        logging.warning("[Go2] MQTT 客户端未注入，跳过 card 清除")
        return
    try:
        _mqtt_client.publish(GO2_CARD_TOPIC, "", retain=True, qos=1)
        logging.info("[Go2] 已清除 retained card: %s", GO2_CARD_TOPIC)
    except Exception as exc:
        logging.error("[Go2] 清除 card 失败: %s", exc)

