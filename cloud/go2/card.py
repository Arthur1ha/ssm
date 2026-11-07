"""Go2 Agent Card and retained MQTT control-plane lifecycle."""

import json
import logging
import time
from copy import deepcopy
from cloud.cards.validation import normalize_card

from cloud.go2.connection import go2
from cloud.go2.actions import ACTIONS, BODY_STATES, TRANSIENT_STATES, fsm_transitions, body_views, velocity_schema
from cloud.go2.state import LIFE_VIEWS, STATE_REASON_LABELS, life_transitions

GO2_CARD_TOPIC = "ssm/agents/go2/card"
GO2_STATUS_TOPIC = "ssm/agents/go2/status"
GO2_STATE_TOPIC = "ssm/agents/go2/state"
_mqtt_client = None
_last_status_online: bool | None = None

# Compatibility imports; the public subset is maintained with the tools.
from cloud.go2.tools.registry import CARD_SKILLS, get_card_skill, card_skills


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
        snapshot = go2.state_snapshot()
        # Bounded projection of the same facts used by HTTP/SSE; no media,
        # model text or second state store lives in the control plane.
        fields = (
            "unit_id", "state_schema_version", "epoch", "revision", "connected",
            "connection_state", "activity_state", "life_state", "body_state",
            "fsm_state", "body_confirmation", "control_mode", "autonomy_mode",
            "life_owner", "life_task_id", "available_actions", "blocked_actions",
            "available_life_actions", "life_blocked_reasons", "pending_action", "ts",
            "last_action", "body_observation_source", "body_observed_at",
            "body_telemetry_fresh", "exploration_enabled", "last_transition",
        )
        state = {key: snapshot[key] for key in fields if key in snapshot}
        state.update(unit_id="go2", ts=snapshot.get("ts", time.time()))
        _mqtt_client.publish(GO2_STATE_TOPIC, json.dumps(state), retain=True, qos=1)
    except Exception as exc:
        logging.warning("[Go2] 发布 state 失败: %s", exc)


def _go2_state_machine() -> dict:
    """Project declared goals and observation edges without inspecting a live adapter."""
    topology = fsm_transitions()
    transitions = []
    interruptible_states = {
        spec.execution_state for spec in ACTIONS.values() if spec.interruptible
    }
    for src, edges in topology.items():
        for trigger, dst in edges.items():
            if trigger == "Move":
                continue  # 有速度的 Move 属于数据面；图上只显示控制状态入口。
            if trigger == "StopMove" and src not in interruptible_states:
                continue  # Safety STOP remains available without promising interruption.
            transitions.append({
                "src": src, "dst": dst, "trigger": trigger,
                "label": ACTIONS[trigger].label,
                "action": trigger,
                "params": {},
                "completion": ACTIONS[trigger].completion,
            })
    # These edges explain observation-driven recovery and completion. They
    # carry no action and never authorize a button or a timed fake completion.
    def observed_transition(src, dst, trigger, label):
        return {"src": src, "dst": dst, "trigger": trigger, "label": label,
                "description": "仅在当前连接收到满足判据的设备反馈时发生。"}

    for spec in ACTIONS.values():
        if spec.execution_state in TRANSIENT_STATES and spec.expected_state:
            transitions.append(observed_transition(spec.execution_state, spec.expected_state,
                                                   "action_completed", "动作完成并确认姿态"))
    transitions.extend([
        observed_transition("standing", "moving", "motion_observed", "检测到移动"),
        observed_transition("moving", "standing", "motion_stationary", "确认停稳"),
    ])
    for src in BODY_STATES:
        transitions.append({
            "src": src, "dst": src,
            "trigger": "EMERGENCY_STOP", "label": "紧急制停",
            "action": "STOP", "params": {}, "safety": True,
        })
    state_machine = {
        "state_field": "fsm_state",
        "connection_field": "connected",
        "primary_region": "body",
        "execution_field": "pending_action",
        "last_execution_field": "last_action",
        "freshness_field": "body_telemetry_fresh",
        "reason_labels": deepcopy(STATE_REASON_LABELS),
        "states": list(BODY_STATES),
        "diagram": {"width": 600, "height": 670, "sections": [
            {"label": "行为", "top": 28},
            {"label": "姿态与动作", "top": 295},
        ]},
        "transitions": transitions,
        # Complete fallback coordinates; never mix a partial legacy layout
        # with circular defaults for newly added states.
        "layout": {state: {"x": .14 + (index % 4) * .24,
                            "y": .14 + (index // 4) * .24}
                   for index, state in enumerate(topology)},
        "views": body_views(),
    }
    # Preserve the established movement anchor for flat consumers; the last
    # grid row has enough space for it without intersecting another node.
    state_machine["layout"]["moving"] = {"x": .50, "y": .82}
    body = {key: deepcopy(state_machine[key]) for key in
            ("states", "transitions", "views", "layout")}
    body.update(id="body", label="身体", state_field="body_state",
                parent={"region": "activity", "state": "active"}, retain_when_inactive=True,
                available_actions_field="available_actions", blocked_actions_field="blocked_actions",
                diagram_group="姿态与动作", node_radius=34)
    # Keep every capability inside the initial viewport. Standing anchors the
    # posture row; all gestures fit below it without horizontal overflow.
    body["layout"] = {value: {"x": .15 + index * .35, "y": 350 / 670}
                      for index, value in enumerate(("lying", "standing", "sitting"))}
    gestures = ("greeting", "stretching", "dancing1", "moving", "dancing2",
                "content", "finger_heart", "pouncing", "jumping")
    body["layout"].update({value: {"x": .15 + (index % 3) * .35,
                                  "y": (445 + (index // 3) * 90) / 670}
                           for index, value in enumerate(gestures)})

    state_machine["regions"] = [
        {"id": "connection", "label": "连接", "state_field": "connection_state",
         "states": ["offline", "connecting", "online"], "transitions": [
             {"src": "offline", "dst": "connecting", "trigger": "connection_requested", "label": "建立连接"},
             {"src": "online", "dst": "connecting", "trigger": "connection_requested", "label": "重新连接"},
             {"src": "connecting", "dst": "online", "trigger": "connection_online", "label": "连接成功"},
             {"src": "connecting", "dst": "offline", "trigger": "connection_failed", "label": "连接失败"},
             {"src": "online", "dst": "offline", "trigger": "connection_offline", "label": "断开或连接失效"},
         ],
         "views": {key: {"label": label, "description": description, "type": "generic"}
                   for key, label, description in (
                       ("offline", "离线", "尚未建立控制连接。"),
                       ("connecting", "连接中", "正在建立控制连接。"),
                       ("online", "在线", "控制连接可用；姿态另行确认。"))}},
        {"id": "activity", "label": "活动", "state_field": "activity_state",
         "diagram_group": "行为", "node_radius": 46,
         "layout": {"rest": {"x": .15, "y": 100 / 670}},
         "available_actions_field": "available_life_actions", "blocked_actions_field": "life_blocked_reasons",
         "parent": {"region": "connection", "state": "online"},
         "states": list(LIFE_VIEWS["activity"]),
         "transitions": life_transitions("activity"),
         "views": deepcopy(LIFE_VIEWS["activity"])},
        {"id": "life", "label": "生活", "state_field": "life_state",
         "diagram_group": "行为", "node_radius": 46,
         "layout": {"idle": {"x": .50, "y": 100 / 670},
                    "play": {"x": .50, "y": 210 / 670},
                    "explore": {"x": .85, "y": 100 / 670}},
         "available_actions_field": "available_life_actions", "blocked_actions_field": "life_blocked_reasons",
         "parent": {"region": "activity", "state": "active"},
         "states": list(LIFE_VIEWS["life"]),
         "transitions": life_transitions("life"),
         "views": deepcopy(LIFE_VIEWS["life"])},
        body,
    ]
    return state_machine


def _build_go2_card() -> dict:
    """构建 Go2 Agent Card dict（静态能力描述，符合 AgentCard schema）。

    card 只含能力，不含 volatile state；online/status 表示真实 WebRTC 连接状态，
    FSM/动作等动态状态经 HTTP /api/go2/* 实时获取。
    """
    from cloud.go2.perception.identity import identity_skill

    card = {
        "card_schema_version": 1,
        "version": "2",
        "unit_id": "go2",
        "device_id": "go2",
        "name": "Go2 机器狗",
        "description": "四足机器人，支持运动控制、导航和视觉感知",
        "agent_type": "robot",
        "services": {"teachings_endpoint": "/api/teachings"},
        "observations": [
            {"id": "vision.person_presence", "name": "看到人",
             "description": "只判断画面中是否有人，不识别身份", "fields": {"detected": "boolean"}},
            {"id": "vision.face_presence", "name": "看到人脸",
             "description": "只判断画面中是否有人脸，不识别身份", "fields": {"detected": "boolean"}},
            {"id": "vision.owner_recognized", "name": "识别到主人",
             "description": "人脸识别确认已登记的主人；需要人脸识别服务可用",
             "available": identity_skill.enabled,
             "fields": {"recognized": "boolean"}},
        ],
        "autonomous_skills": ["go2_sport"],
        "online": False,
        "transport": {
            "kind": "http",
            "endpoint": "/api/go2/chat",
            "command_endpoint": "/api/go2/commands",
            "state_stream": "/api/go2/connection/stream",
        },
        "skills": card_skills(),
        "suggestions": [
            "向我挥挥手",
            "看看你面前有什么",
            "识别一下你面前的人",
            "有哪些已保存的地点？",
        ],
        # 动态状态不进 card（card=静态能力）；FSM/动作经 HTTP /api/go2/* 实时获取
        "state": {},
        "state_machine": _go2_state_machine(),
        "modes": [{
            "id": "autonomy",
            "label": "自主性",
            "state_field": "autonomy_mode",
            "options": [
                {"value": "manual",       "label": "完全遥控", "description": "由用户/编排器遥控决定行动"},
                {"value": "free_explore", "label": "允许自主探索", "description": "允许现有自主策略发起探索，当前生活状态另行显示；休息和显式停止后不会自动重放任务"},
            ],
            "get": "/api/go2/autonomy",
            "set": "/api/go2/autonomy",
            "set_action": "set_mode",
        }],
        "telemetry": [
            {"key": "fsm_state", "label": "兼容动作状态"},
            {"key": "body_state",  "label": "身体状态"},
            {"key": "control_mode", "label": "控制模式"},
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
            {"type": "video", "states": [], "placement": "media", "order": 10, "endpoint": "/api/go2/video", "requires_connection": True},
            {"type": "map", "states": [], "placement": "media", "order": 15, "endpoint": "/api/go2/map.png", "requires_connection": True},
            {"type": "joystick", "states": ["standing", "moving"], "placement": "controls", "order": 20, "action": "velocity",
             "state_field": "body_state", "enabled_when": {"control_mode": "remote"},
             "node_state": "moving", "enter_action": "EnterMoveControl", "exit_action": "StopMove",
             "available_actions_field": "available_actions", "blocked_actions_field": "blocked_actions", "availability_key": "Move", "params_schema": velocity_schema()},
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

