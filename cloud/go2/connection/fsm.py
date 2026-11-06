import logging
import math

from cloud.go2.actions import ACTIONS, TRANSIENT_STATES, fsm_transitions

logger = logging.getLogger(__name__)


class CommandBlocked(ValueError):
    """A valid command is not allowed in the current device state."""

_FSM_NEXT = fsm_transitions()
_FSM_AVAILABLE = {"offline": [], "connecting": [],
                  **{state: list(edges) for state, edges in _FSM_NEXT.items()}}
_TRANSIENT_STATES = set(TRANSIENT_STATES)


class Go2FSM:
    """Body transition rules; only supported observations confirm completion."""

    def __init__(self) -> None:
        super().__init__()
        self._fsm_state: str = "offline"
        self._last_progress: float = 0.0
        self.pending_action: dict | None = None
        self.body_confirmation = "unknown"

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
        if self.pending_action is not None:
            action = ACTIONS[self.pending_action["command"]]
            return ["StopMove"] if action.interruptible and self._fsm_state not in {"offline", "connecting"} else []
        return _FSM_AVAILABLE.get(self._fsm_state, [])

    def fsm_next(self, cmd: str) -> str | None:
        """查询在当前状态下执行 cmd 后的目标状态，无对应转移返回 None。"""
        return _FSM_NEXT.get(self._fsm_state, {}).get(cmd)

    def _fsm_on_progress(self, progress: float) -> None:
        """Record uncorrelated telemetry without completing a body command.

        Sport state does not carry the command request ID. A falling sample may
        belong to an earlier action; the probe records also show progress=0 for
        visibly executed actions. It cannot authorize a dependent action.
        """
        if type(progress) in (int, float) and math.isfinite(progress):
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
            self.pending_action = None
            self.body_confirmation = "observed"
            self.fsm_state = target
            waiter.set_result({"result": "ok", "confirmation": "body_height",
                               "fsm_state": target, "body_height": body_height})
