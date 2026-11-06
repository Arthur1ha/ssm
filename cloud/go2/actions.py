"""Go2 body actions, their wire mappings and the evidence they actually support.

RPC acceptance is deliberately separate from physical completion.  The local
probe records verify execution of five new Sport commands, but provide neither
a sitting-mode discriminator nor a request-correlated completion signal.
"""

from dataclasses import dataclass


BODY_STATES = (
    "standing", "lying", "sitting", "moving", "greeting", "stretching",
    "content", "dancing1", "dancing2", "finger_heart", "pouncing", "jumping",
)
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


def _gesture(command: str, label: str, api_id: int, state: str,
             *, interruptible: bool = False) -> ActionSpec:
    return ActionSpec(command, label, "sport", api_id, ("standing",), state,
                      "standing", "unavailable", interruptible=interruptible)


ACTIONS = {spec.command: spec for spec in (
    ActionSpec("StandUp", "起立", "sport", 1004, ("lying",), None,
               "standing", "body_height", timeout_s=20.0),
    ActionSpec("StandDown", "趴下", "sport", 1005, ("standing",), None,
               "lying", "body_height", timeout_s=20.0),
    ActionSpec("StopMove", "停止", "sport", 1003,
               (*BODY_STATES, "unknown"), None, None, "unavailable"),
    _gesture("Hello", "挥手", 1016, "greeting", interruptible=True),
    _gesture("Stretch", "伸展", 1017, "stretching", interruptible=True),
    _gesture("Dance1", "舞蹈1", 1022, "dancing1", interruptible=True),
    _gesture("Dance2", "舞蹈2", 1023, "dancing2", interruptible=True),
    ActionSpec("Sit", "坐下", "sport", 1009, ("standing",), None,
               "sitting", "unavailable"),
    ActionSpec("RiseSit", "坐姿起身", "sport", 1010, ("sitting",), None,
               "standing", "unavailable"),
    _gesture("Content", "Content", 1020, "content"),
    _gesture("FingerHeart", "比心", 1036, "finger_heart"),
    _gesture("FrontPounce", "前扑", 1032, "pouncing"),
    _gesture("FrontJump", "前跳", 1031, "jumping"),
    ActionSpec("Move", "移动", "sport", 1008, ("standing", "moving"),
               "moving", None, "unavailable", interruptible=True,
               parameter_keys=("x", "y", "z")),
    ActionSpec("EnterMoveControl", "进入遥控", "local", None, ("standing",),
               "moving", None, "local"),
    ActionSpec("BalanceStand", "平衡站立", "sport", 1002, ("standing",),
               None, "standing", "unavailable"),
)}

SPORT_COMMANDS = tuple(command for command in ACTIONS
                       if command not in {"Move", "EnterMoveControl", "BalanceStand"})
MOTION_COMMANDS = frozenset(ACTIONS)


def fsm_transitions() -> dict[str, dict[str, str]]:
    """Declared topology; a target is a goal, never completion evidence."""
    transitions = {state: {} for state in (*BODY_STATES, "unknown")}
    for action in ACTIONS.values():
        if action.command == "BalanceStand":
            continue  # Internal connection activation, not another public action.
        for source in action.source_states:
            if action.command == "StopMove":
                target = source if source in {"standing", "lying", "sitting"} else "unknown"
            else:
                target = action.execution_state or action.expected_state
            transitions[source][action.command] = target
    return transitions
