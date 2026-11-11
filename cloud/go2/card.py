"""Go2 Agent Card and retained MQTT control-plane lifecycle."""

import json
import logging
import time
from copy import deepcopy
from math import cos, pi, sin
from cloud.cards.validation import normalize_card

from cloud.go2.connection import go2
from cloud.go2.actions import ACTIONS, SPORT_COMMANDS, BODY_STATES, TRANSIENT_STATES, fsm_transitions, body_views, velocity_schema
from cloud.go2.state import LIFE_VIEWS, STATE_REASON_LABELS, life_transitions

GO2_CARD_TOPIC = "ssm/agents/go2/card"
GO2_STATUS_TOPIC = "ssm/agents/go2/status"
GO2_STATE_TOPIC = "ssm/agents/go2/state"
_mqtt_client = None
_last_status_online: bool | None = None
TRAINING_OBSERVATIONS = (
    "vision.person_presence", "vision.face_presence", "vision.owner_recognized",
)

from cloud.go2.tools.registry import card_skills


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
            "body_confirmation", "control_mode", "autonomy_mode",
            "life_owner", "life_task_id", "available_actions", "blocked_actions",
            "available_life_actions", "life_blocked_reasons", "pending_action", "ts",
            "last_action", "body_observation_source", "body_observed_at",
            "available_manual_actions", "blocked_manual_actions",
            "body_telemetry_fresh", "exploration_enabled", "exploration_owner", "last_transition",
        )
        state = {key: snapshot[key] for key in fields if key in snapshot}
        state.update(unit_id="go2", ts=snapshot.get("ts", time.time()))
        _mqtt_client.publish(GO2_STATE_TOPIC, json.dumps(state), retain=True, qos=1)
    except Exception as exc:
        logging.warning("[Go2] 发布 state 失败: %s", exc)


def _go2_state_machine() -> dict:
    """Project declared goals and observation edges without inspecting a live adapter."""
    topology = fsm_transitions()
    views = body_views()
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
            spec = ACTIONS[trigger]
            condition = f"收到{spec.label}指令"
            if spec.expected_state and not spec.execution_state and spec.completion == "joint_posture":
                condition += "，且设备确认" + views[dst]["label"]
            transitions.append({
                "src": src, "dst": dst, "trigger": trigger,
                "label": ACTIONS[trigger].label,
                "trigger_condition": condition,
                "action": trigger,
                "params": {},
                "completion": ACTIONS[trigger].completion,
                # Continuous motion/remote control is outside first-version training.
                **({"training_disabled": True} if trigger in {'Move', 'EnterMoveControl', 'BalanceStand'} else {}),
                # Observations can request any trainable discrete action. The
                # original action contract still owns posture and safety.
                **({"training_events": list(TRAINING_OBSERVATIONS)}
                   if trigger not in {'Move', 'EnterMoveControl', 'BalanceStand', 'StopMove'} else {}),
                **({"dispatch_only": True}
                   if trigger in SPORT_COMMANDS and trigger != 'StopMove' else {}),
            })
    # These edges explain observation-driven recovery and completion. They
    # carry no action and never authorize a button or a timed fake completion.
    def observed_transition(src, dst, trigger, label, condition):
        return {"src": src, "dst": dst, "trigger": trigger, "label": label,
                "trigger_condition": condition}

    for spec in ACTIONS.values():
        if spec.execution_state in TRANSIENT_STATES and spec.expected_state:
            transitions.append(observed_transition(spec.execution_state, spec.expected_state,
                                                   "action_completed", "动作执行完毕",
                                                   "设备确认动作完成，且已恢复" + views[spec.expected_state]["label"]))
    transitions.extend([
        observed_transition("standing", "moving", "motion_observed", "开始移动", "设备反馈正在移动"),
        observed_transition("moving", "standing", "motion_stationary", "停止移动", "设备反馈已停止移动"),
    ])
    for src in BODY_STATES:
        transitions.append({
            "src": src, "dst": src,
            "trigger": "EMERGENCY_STOP", "label": "紧急制停",
            "action": "STOP", "params": {}, "safety": True,
        })
    state_machine = {
        "connection_field": "connected",
        "primary_region": "body",
        "execution_field": "pending_action",
        "last_execution_field": "last_action",
        "freshness_field": "body_telemetry_fresh",
        "reason_labels": deepcopy(STATE_REASON_LABELS),
        "diagram": {"width": 600, "height": 670, "sections": [
            {"label": "行为", "top": 28},
            {"label": "姿态与动作", "top": 295},
        ]},
    }
    body = {"states": list(BODY_STATES), "transitions": transitions, "views": views}
    body.update(id="body", label="身体", state_field="body_state",
                prepare_action={"via": "standing", "action": "SPORT"},
                parent={"region": "activity", "state": "active"}, retain_when_inactive=True,
                available_actions_field="available_actions", blocked_actions_field="blocked_actions",
                manual_available_actions_field="available_manual_actions",
                manual_blocked_actions_field="blocked_manual_actions",
                diagram_group="姿态与动作", node_radius=34)
    # Standing is the shared hub. Give each surrounding action its own radial
    # corridor instead of routing multiple rows through the same grid lanes.
    body["layout"] = {"standing": {"x": .5, "y": 475 / 670}}
    ring = ("moving", "sitting", "stretching", "dancing1", "content", "dancing2",
            "finger_heart", "pouncing", "jumping", "greeting", "lying")
    for index, value in enumerate(ring):
        angle = -pi / 2 + index * 2 * pi / len(ring)
        body["layout"][value] = {"x": (300 + 220 * cos(angle)) / 600,
                                 "y": (475 + 145 * sin(angle)) / 670}

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
         "layout": {"rest": {"x": .30, "y": 85 / 670}},
         "available_actions_field": "available_life_actions", "blocked_actions_field": "life_blocked_reasons",
         "parent": {"region": "connection", "state": "online"},
         "states": list(LIFE_VIEWS["activity"]),
         "transitions": life_transitions("activity"),
         "views": deepcopy(LIFE_VIEWS["activity"])},
        {"id": "life", "label": "生活", "state_field": "life_state",
         "diagram_group": "行为", "node_radius": 46,
         "layout": {"idle": {"x": .70, "y": 85 / 670},
                    "play": {"x": .70, "y": 205 / 670},
                    "explore": {"x": .30, "y": 205 / 670}},
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

    card 只含当前静态能力；服务在线、WebRTC 连接及动作状态由独立实时通道提供。
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
        "state_machine": _go2_state_machine(),
        "modes": [{
            "id": "autonomy",
            "label": "自主性",
            "state_field": "autonomy_mode",
            "options": [
                {"value": "manual",       "label": "待命", "description": "不自主巡游，仍响应用户命令和已教导的事件动作"},
                {"value": "free_explore", "label": "允许自主探索", "description": "自主探索启动后持续显示探索，互动任务接管时显示互动；休息和显式停止后不会自动重放任务"},
            ],
            "get": "/api/go2/autonomy",
            "set_action": "set_mode",
        }],
        "telemetry": [
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
    from cloud.agents.common.training.catalog import declare_transition_ids
    return declare_transition_ids(normalize_card(card))


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
