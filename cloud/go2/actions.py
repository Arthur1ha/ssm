"""Go2 body actions, wire mappings and physical-observation completion policy.

RPC acceptance remains separate from completion. Live LOW_STATE joints and
IMU can distinguish standing/sitting and observe action motion followed by a
stable target posture. Missing joint feedback never becomes RPC-only success.
"""

from dataclasses import dataclass


BODY_STATES = (
    "standing", "lying", "sitting", "moving", "greeting", "stretching",
    "content", "dancing1", "dancing2", "finger_heart", "pouncing", "jumping",
)
STABLE_BODY_STATES = frozenset(BODY_STATES[:3])
TRANSIENT_STATES = frozenset(BODY_STATES[4:])


@dataclass(frozen=True)
class ActionSpec:
    command: str
    label: str
    catalog: str
    api_id: int | None
    source_states: tuple[str, ...]
    execution_state: str | None
    expected_state: str | None
    completion: str
    timeout_s: float = 5.0
    interruptible: bool = False
    parameter_keys: tuple[str, ...] = ()
    description: str = "动作或姿态以真实反馈为准；请求接受不表示完成。"
    preview: str | None = None


def _gesture(command: str, label: str, api_id: int, state: str,
             *, interruptible: bool = False, timeout_s: float = 25.0,
             description: str = "动作以真实反馈为准；请求接受不表示完成。",
             preview: str | None = None) -> ActionSpec:
    return ActionSpec(command, label, "sport", api_id, ("standing",), state,
                      "standing", "joint_posture", timeout_s=timeout_s, interruptible=interruptible,
                      description=description, preview=preview or state)


ACTIONS = {spec.command: spec for spec in (
    ActionSpec("StandUp", "起立", "sport", 1004, ("lying",), None,
               "standing", "joint_posture", timeout_s=20.0),
    ActionSpec("StandDown", "趴下", "sport", 1005, ("standing",), None,
               "lying", "joint_posture", timeout_s=20.0),
    ActionSpec("StopMove", "停止", "sport", 1003,
               BODY_STATES, None, None, "unavailable"),
    _gesture("Hello", "挥手", 1016, "greeting", interruptible=True, description="抬起前腿执行问候动作，等待设备确认完成。"),
    _gesture("Stretch", "伸展", 1017, "stretching", interruptible=True, description="降低身体前部并完成伸展动作。"),
    _gesture("Dance1", "舞蹈1", 1022, "dancing1", interruptible=True, timeout_s=60.0, preview="dance-left"),
    _gesture("Dance2", "舞蹈2", 1023, "dancing2", interruptible=True, timeout_s=60.0, preview="dance-right"),
    ActionSpec("Sit", "坐下", "sport", 1009, ("standing",), None,
               "sitting", "joint_posture", timeout_s=20.0),
    ActionSpec("RiseSit", "坐姿起身", "sport", 1010, ("sitting",), None,
               "standing", "joint_posture", timeout_s=20.0),
    _gesture("Content", "开心", 1020, "content"),
    _gesture("FingerHeart", "比心", 1036, "finger_heart"),
    _gesture("FrontPounce", "前扑", 1032, "pouncing"),
    _gesture("FrontJump", "前跳", 1031, "jumping"),
    ActionSpec("Move", "移动", "sport", 1008, ("standing", "moving"),
               "moving", None, "unavailable", interruptible=True,
               parameter_keys=("x", "y", "z")),
    ActionSpec("EnterMoveControl", "准备移动", "local", None, ("standing",),
               None, "standing", "local"),
    ActionSpec("BalanceStand", "平衡站立", "sport", 1002, ("standing",),
               None, "standing", "unavailable"),
)}

SPORT_COMMANDS = tuple(command for command in ACTIONS
                       if command not in {"Move", "EnterMoveControl", "BalanceStand"})
MOTION_COMMANDS = frozenset(ACTIONS)


def fsm_transitions() -> dict[str, dict[str, str]]:
    """Declared topology; a target is a goal, never completion evidence."""
    transitions = {state: {} for state in BODY_STATES}
    for action in ACTIONS.values():
        if action.command == "BalanceStand":
            continue  # Internal connection activation, not another public action.
        for source in action.source_states:
            if action.command == "StopMove":
                target = source  # Stop is a request, not evidence of a new posture.
            else:
                target = action.execution_state or action.expected_state
            transitions[source][action.command] = target
    return transitions


# Timed movement and continuous velocity share physical axis limits. Duration
# remains a timed-tool policy, distinct from action completion deadlines.
VELOCITY_LIMITS = {"x": 0.5, "y": 0.5, "z": 0.8}
MOVE_DEFAULT_SPEED = 0.3
MOVE_DEFAULT_DURATION = 1.0
MOVE_MAX_DURATION = 10.0
MOVE_DIRECTIONS = ("forward", "backward", "left", "right", "turn_left", "turn_right")


def sport_tool_schema() -> dict:
    return {"type": "object", "properties": {"cmd": {
        "type": "string", "enum": list(SPORT_COMMANDS),
        "x-enum-labels": {cmd: ACTIONS[cmd].label for cmd in SPORT_COMMANDS},
    }}, "required": ["cmd"], "additionalProperties": False}


def sport_description() -> str:
    return "执行预定义动作：" + "；".join(
        f"{ACTIONS[cmd].label}({cmd})：{ACTIONS[cmd].description}" for cmd in SPORT_COMMANDS
    ) + "；accepted 仅表示请求已接受，只有完成证据确认后才可继续依赖动作"


def move_tool_schema() -> dict:
    return {"type": "object", "properties": {
        "direction": {"type": "string", "enum": list(MOVE_DIRECTIONS)},
        "speed": {"type": "number", "exclusiveMinimum": 0,
                  "maximum": min(VELOCITY_LIMITS.values()),
                  "default": min(MOVE_DEFAULT_SPEED, min(VELOCITY_LIMITS.values()))},
        "duration": {"type": "number", "exclusiveMinimum": 0,
                     "maximum": MOVE_MAX_DURATION, "default": min(MOVE_DEFAULT_DURATION, MOVE_MAX_DURATION)},
    }, "required": ["direction"], "additionalProperties": False}


def velocity_schema() -> dict:
    return {"type": "object", "properties": {
        axis: {"type": "number", "minimum": -limit, "maximum": limit, "default": 0}
        for axis, limit in VELOCITY_LIMITS.items()
    }, "additionalProperties": False}


BODY_VIEWS = {
    "lying": {"label": "趴卧", "description": "身体降低并保持稳定，等待起立指令。", "type": "robot_pose", "pose": "lying"},
    "standing": {"label": "站立", "description": "四足稳定站立，可以移动或执行动作。", "type": "robot_pose", "pose": "standing"},
    "sitting": {"label": "坐下", "description": "已确认坐姿，等待坐姿起身指令。", "type": "robot_pose", "pose": "sitting"},
    "moving": {"label": "移动", "description": "身体正在移动；遥控入口与实际移动分别显示，停稳以真实反馈为准。", "type": "robot_pose", "pose": "moving"},
}


def body_views() -> dict:
    from copy import deepcopy
    views = deepcopy(BODY_VIEWS)
    for action in ACTIONS.values():
        if action.execution_state in TRANSIENT_STATES:
            views[action.execution_state] = {
                "label": action.label, "description": action.description,
                "type": "robot_pose", "pose": action.preview or action.execution_state,
            }
    return views


LED_COLORS = ("white", "red", "yellow", "blue", "green", "cyan", "purple")
LED_DEFAULT_COLOR = "white"
LED_DEFAULT_DURATION = 60
LED_MAX_DURATION = 3600


def led_tool_schema() -> dict:
    return {"type": "object", "properties": {
        "color": {"type": "string", "enum": list(LED_COLORS), "default": LED_DEFAULT_COLOR},
        "duration": {"type": "integer", "minimum": 1, "maximum": LED_MAX_DURATION,
                     "default": LED_DEFAULT_DURATION},
    }, "required": [], "additionalProperties": False}
