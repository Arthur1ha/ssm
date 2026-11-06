"""Go2 Agent Card and retained MQTT control-plane lifecycle."""

import json
import logging
import time
from copy import deepcopy
from cloud.cards.validation import normalize_card

from cloud.go2.connection import go2
from cloud.go2.actions import ACTIONS, BODY_STATES

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
    "go2_life": {
        "tool_name": "go2_life",
        "name": "休息、唤醒与探索",
        "description": "待命稳定时休息，保持当前姿态；唤醒回待命，不重放探索。开启探索沿用管理员权限。",
        "tags": ["activity", "robot"],
        "invoke": {"action": "LIFE"},
    },
    "go2_navigate": {
        "tool_name": "go2_navigate_to",
        "name": "导航到命名地点",
        "description": "仅在当前会话拥有可信定位与已知安全路线时导航；条件不足会拒绝移动",
        "tags": ["navigation", "robot"],
        "invoke": {"action": "NAVIGATE"},
    },
    "go2_visual_navigate": {
        "tool_name": "go2_navigate_to_visible_target",
        "name": "视觉目标导航",
        "description": "用相机与雷达定位可见物体，沿实时地图导航到附近停靠点并重新观察确认",
        "tags": ["navigation", "vision", "motion", "robot"],
        "invoke": {"action": "VISUAL_NAVIGATE"},
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
    "go2_observe": {
        "tool_name": "go2_observe",
        "name": "观察当前画面",
        "description": "通过摄像头描述当前看到的场景",
        "tags": ["vision", "robot"],
        "invoke": {"action": "OBSERVE"},
    },
    "go2_status": {
        "tool_name": "go2_status",
        "name": "查看机器狗状态",
        "description": "读取连接、姿态与运动状态",
        "tags": ["status", "robot"],
        "invoke": {"action": "STATUS"},
    },
    "go2_set_led": {
        "tool_name": "go2_set_led",
        "name": "设置机身灯",
        "description": "设置机器狗机身灯的颜色和时长",
        "tags": ["light", "robot"],
        "invoke": {"action": "SET_LED"},
    },
    "go2_list_locations": {
        "tool_name": "go2_list_locations",
        "name": "查看已保存地点",
        "description": "列出机器狗可导航的命名地点",
        "tags": ["navigation", "robot"],
        "invoke": {"action": "LIST_LOCATIONS"},
    },
    "go2_tag_location": {
        "tool_name": "go2_tag_location",
        "name": "保存当前地点",
        "description": "管理员将当前定位保存为可导航的命名地点",
        "tags": ["navigation", "persistent", "robot"],
        "invoke": {"action": "TAG_LOCATION"},
    },
    "go2_list_teachings": {
        "tool_name": "go2_list_teachings",
        "name": "查看已保存教导",
        "description": "查看机器狗的教导和自动执行状态",
        "tags": ["memory", "robot"],
        "invoke": {"action": "LIST_TEACHINGS"},
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
        from cloud.go2.tools.registry import get_tool_spec
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


_GO2_FSM_LABELS = {command: action.label for command, action in ACTIONS.items()}


def _go2_state_machine() -> dict:
    """依据 connection/fsm.py 的 _FSM_NEXT 生成静态状态机拓扑（单一真相）。

    延迟导入以避免在测试 stub 环境中与 cloud.go2.connection 补丁冲突。
    """
    from cloud.go2.connection.fsm import _FSM_AVAILABLE, _FSM_NEXT, _TRANSIENT_STATES  # noqa: PLC0415
    transitions = []
    interruptible_states = {
        spec.execution_state for spec in ACTIONS.values() if spec.interruptible
    }
    for src, edges in _FSM_NEXT.items():
        for trigger, dst in edges.items():
            if trigger == "Move":
                continue  # 有速度的 Move 属于数据面；图上只显示控制状态入口。
            if trigger == "StopMove" and src not in interruptible_states:
                continue  # Safety STOP remains available without promising interruption.
            transitions.append({
                "src": src, "dst": dst, "trigger": trigger,
                "label": "停止" if src in _TRANSIENT_STATES else _GO2_FSM_LABELS.get(trigger, trigger),
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
        if spec.execution_state in _TRANSIENT_STATES and spec.expected_state:
            transitions.append(observed_transition(spec.execution_state, spec.expected_state,
                                                   "action_completed", "动作完成并确认姿态"))
    transitions.extend([
        observed_transition("standing", "moving", "motion_observed", "检测到移动"),
        observed_transition("moving", "standing", "motion_stationary", "确认停稳"),
    ])
    for src in _FSM_AVAILABLE:
        if src in {"offline", "connecting"}:
            continue
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
        "states": [s for s in _FSM_AVAILABLE if s not in {"offline", "connecting"}],
        "diagram": {"width": 600, "height": 670, "sections": [
            {"label": "行为", "top": 28},
            {"label": "姿态与动作", "top": 295},
        ]},
        "transitions": transitions,
        # Complete fallback coordinates; never mix a partial legacy layout
        # with circular defaults for newly added states.
        "layout": {state: {"x": .14 + (index % 4) * .24,
                            "y": .14 + (index // 4) * .24}
                   for index, state in enumerate(_FSM_NEXT)},
        "views": {
            "offline": {"label": "未连接", "description": "机器狗尚未建立控制连接。", "type": "robot_pose", "pose": "offline"},
            "connecting": {"label": "正在连接", "description": "正在与机器狗建立实时控制通道。", "type": "robot_pose", "pose": "offline"},
            "lying": {"label": "趴卧", "description": "身体降低并保持稳定，等待起立指令。", "type": "robot_pose", "pose": "lying"},
            "standing": {"label": "站立", "description": "四足稳定站立，可以移动或执行动作。", "type": "robot_pose", "pose": "standing"},
            "moving": {"label": "移动中", "description": "身体正在移动；遥控入口与实际移动分别显示，停稳以真实反馈为准。", "type": "robot_pose", "pose": "moving"},
            "greeting": {"label": "挥手", "description": "抬起前腿执行问候动作。", "type": "robot_pose", "pose": "greeting"},
            "stretching": {"label": "伸展", "description": "降低身体前部并完成伸展动作。", "type": "robot_pose", "pose": "stretching"},
            "dancing1": {"label": "舞蹈1", "description": "正在执行第一组节奏动作。", "type": "robot_pose", "pose": "dance-left"},
            "dancing2": {"label": "舞蹈2", "description": "正在执行第二组节奏动作。", "type": "robot_pose", "pose": "dance-right"},
        },
    }
    # Preserve the established movement anchor for flat consumers; the last
    # grid row has enough space for it without intersecting another node.
    state_machine["layout"]["moving"] = {"x": .50, "y": .82}
    # New body nodes are capabilities. Their destination is a requested goal,
    # not evidence that the hardware has reached it.
    for action in ACTIONS.values():
        state = action.execution_state or action.expected_state
        if state in BODY_STATES and state not in state_machine["views"]:
            state_machine["views"][state] = {
                "label": action.label, "description": "动作或姿态以真实反馈为准；请求接受不表示完成。",
                "type": "robot_pose", "pose": state,
            }
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

    def life_transition(src, dst, operation, label):
        return {"src": src, "dst": dst, "trigger": operation, "label": label,
                "action": "LIFE", "params": {"action": operation},
                "availability_key": operation,
                **({"required_actor": "admin"} if operation == "start_explore" else {})}

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
         "states": ["active", "rest"],
         "transitions": [life_transition("active", "rest", "request_rest", "休息"),
                         life_transition("rest", "active", "wake", "唤醒")],
         "views": {"active": {"label": "可活动", "description": "生活与身体区域同时有效。", "type": "generic"},
                   "rest": {"label": "休息", "description": "保持连接与当前稳定姿态，暂停自主活动。", "type": "generic"}}},
        {"id": "life", "label": "生活", "state_field": "life_state",
         "diagram_group": "行为", "node_radius": 46,
         "layout": {"idle": {"x": .50, "y": 100 / 670},
                    "play": {"x": .50, "y": 210 / 670},
                    "explore": {"x": .85, "y": 100 / 670}},
         "available_actions_field": "available_life_actions", "blocked_actions_field": "life_blocked_reasons",
         "parent": {"region": "activity", "state": "active"},
         "states": ["idle", "play", "explore"],
         "transitions": [life_transition("idle", "explore", "start_explore", "开启探索"),
                         life_transition("explore", "idle", "stop_explore", "停止探索"),
                         {**life_transition("idle", "idle", "stop_explore", "停止探索"), "available_only": True},
                         {"src": "idle", "dst": "play", "trigger": "task_accepted", "label": "接纳任务"},
                         {"src": "play", "dst": "idle", "trigger": "task_finished", "label": "互动结束"},
                         {"src": "idle", "dst": "explore", "trigger": "task_accepted", "label": "接纳探索"},
                         {"src": "explore", "dst": "idle", "trigger": "task_finished", "label": "探索结束"}],
         "views": {"idle": {"label": "待命", "description": "等待用户或现有自主策略接纳任务。", "type": "generic"},
                   "play": {"label": "互动", "description": "已接纳的互动任务正在进行。", "type": "generic"},
                   "explore": {"label": "探索", "description": "已接纳探索任务，停下观察时也可保持探索。", "type": "generic"}}},
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
             "available_actions_field": "available_actions", "blocked_actions_field": "blocked_actions", "availability_key": "Move"},
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

