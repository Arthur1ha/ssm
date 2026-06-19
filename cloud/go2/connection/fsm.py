import logging

logger = logging.getLogger(__name__)

_GESTURES = ["Hello", "Stretch", "Dance1", "Dance2"]
_GESTURE_NEXT = {"Hello": "greeting", "Stretch": "stretching",
                 "Dance1": "dancing1", "Dance2": "dancing2"}

_FSM_AVAILABLE: dict[str, list[str]] = {
    "offline":    [],
    "connecting": [],
    "lying":      ["StandUp"],
    "standing":   ["StandDown", "Move"] + _GESTURES,
    "moving":     ["StopMove"] + _GESTURES,
    "greeting":   ["Move", "StopMove"] + _GESTURES,
    "stretching": ["Move", "StopMove"] + _GESTURES,
    "dancing1":   ["Move", "StopMove"] + _GESTURES,
    "dancing2":   ["Move", "StopMove"] + _GESTURES,
}

_FSM_NEXT: dict[str, dict[str, str]] = {
    "lying":      {"StandUp": "standing"},
    "standing":   {"StandDown": "lying", "Move": "moving", **_GESTURE_NEXT},
    "moving":     {"StopMove": "standing", **_GESTURE_NEXT},
    "greeting":   {"StopMove": "standing", "Move": "moving", **_GESTURE_NEXT},
    "stretching": {"StopMove": "standing", "Move": "moving", **_GESTURE_NEXT},
    "dancing1":   {"StopMove": "standing", "Move": "moving", **_GESTURE_NEXT},
    "dancing2":   {"StopMove": "standing", "Move": "moving", **_GESTURE_NEXT},
}

# 瞬时动作态：做完（进度事件）自动回 standing
_TRANSIENT_STATES = {"greeting", "stretching", "dancing1", "dancing2"}


class Go2FSM:
    """Go2 状态机契约：状态/可用动作/转移查询，以及进度事件触发的返回。"""

    def __init__(self) -> None:
        super().__init__()
        self._fsm_state: str = "offline"
        self._last_progress: float = 0.0

    @property
    def fsm_state(self) -> str:
        return self._fsm_state

    @fsm_state.setter
    def fsm_state(self, new: str) -> None:
        if new != self._fsm_state:
            logger.info("[Go2/FSM] %s → %s", self._fsm_state, new)
        self._fsm_state = new

    @property
    def available_actions(self) -> list[str]:
        """当前状态下可执行的动作列表。"""
        return _FSM_AVAILABLE.get(self._fsm_state, [])

    def fsm_next(self, cmd: str) -> str | None:
        """查询在当前状态下执行 cmd 后的目标状态，无对应转移返回 None。"""
        return _FSM_NEXT.get(self._fsm_state, {}).get(cmd)

    def _fsm_on_progress(self, progress: float) -> None:
        """进度从'在动'(>0.05)落到'停了'(<0.05) → 瞬时动作态做完，自动回 standing。"""
        if (self._fsm_state in _TRANSIENT_STATES
                and self._last_progress > 0.05
                and progress < 0.05):
            self.fsm_state = "standing"
        self._last_progress = progress
