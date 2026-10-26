import logging
import math

logger = logging.getLogger(__name__)


class CommandBlocked(ValueError):
    """A valid command is not allowed in the current device state."""

_GESTURE_NEXT = {"Hello": "greeting", "Stretch": "stretching",
                 "Dance1": "dancing1", "Dance2": "dancing2"}

# 站立是动作枢纽；移动须先停止，动作完成后回到站立。
_FSM_NEXT: dict[str, dict[str, str]] = {
    "lying":      {"StandUp": "standing"},
    "standing":   {"StandDown": "lying", "EnterMoveControl": "moving", "Move": "moving", **_GESTURE_NEXT},
    "moving":     {"StopMove": "standing"},
    "greeting":   {"StopMove": "standing"},
    "stretching": {"StopMove": "standing"},
    "dancing1":   {"StopMove": "standing"},
    "dancing2":   {"StopMove": "standing"},
}
_FSM_AVAILABLE: dict[str, list[str]] = {
    "offline": [], "connecting": [],
    **{state: list(edges) for state, edges in _FSM_NEXT.items()},
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
            publisher = getattr(self, "_peer_state_callback", None)
            if publisher is not None:
                publisher()
            return
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
            waiter = getattr(self, "_completion", None)
            if waiter is not None and not waiter.done():
                waiter.set_result({"result": "ok", "confirmation": "progress", "fsm_state": "standing"})
        self._last_progress = progress

    def _fsm_on_posture(self, body_height: object) -> None:
        """Confirm stand transitions only from fresh, stable robot telemetry."""
        waiter = getattr(self, "_completion", None)
        target = getattr(self, "_completion_target", None)
        if waiter is None or waiter.done() or target not in {"lying", "standing"}:
            return
        if type(body_height) not in (int, float) or not math.isfinite(body_height):
            self._completion_samples = 0
            return
        reached = body_height <= 0.12 if target == "lying" else body_height >= 0.25
        self._completion_samples = self._completion_samples + 1 if reached else 0
        if self._completion_samples >= 3:
            waiter.set_result({"result": "ok", "confirmation": "body_height",
                               "fsm_state": target, "body_height": body_height})
