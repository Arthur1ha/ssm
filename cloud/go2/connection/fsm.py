import asyncio

_FSM_AVAILABLE: dict[str, list[str]] = {
    "offline":    [],
    "connecting": [],
    "lying":      ["StandUp"],
    "standing":   ["StandDown", "Move", "StopMove", "Hello", "Stretch", "Dance1", "Dance2"],
    "moving":     ["Move", "StopMove"],
    "executing":  ["StopMove", "Hello", "Stretch", "Dance1", "Dance2"],
}

_FSM_NEXT: dict[str, dict[str, str]] = {
    "lying":     {"StandUp":   "standing"},
    "standing":  {"StandDown": "lying", "Move": "moving",
                  "Hello": "executing", "Stretch": "executing",
                  "Dance1": "executing", "Dance2": "executing"},
    "moving":    {"StopMove": "standing"},
    "executing": {"StopMove": "standing",
                  "Hello": "executing", "Stretch": "executing",
                  "Dance1": "executing", "Dance2": "executing"},
}

_EXEC_RESET_DELAY = 12.0


class Go2FSM:
    def __init__(self) -> None:
        super().__init__()
        self.fsm_state: str = "offline"
        self._exec_reset_task: asyncio.Task | None = None
        self._last_progress: float = 0.0

    @property
    def available_actions(self) -> list[str]:
        return _FSM_AVAILABLE.get(self.fsm_state, [])

    def fsm_next(self, cmd: str) -> str | None:
        return _FSM_NEXT.get(self.fsm_state, {}).get(cmd)

    def _fsm_on_progress(self, progress: float) -> None:
        if (self.fsm_state == "executing"
                and self._last_progress > 0.05
                and progress < 0.05):
            self.fsm_state = "standing"
            if self._exec_reset_task and not self._exec_reset_task.done():
                self._exec_reset_task.cancel()
        self._last_progress = progress

    def _schedule_exec_reset(self) -> None:
        if self._exec_reset_task and not self._exec_reset_task.done():
            self._exec_reset_task.cancel()

        async def _reset():
            await asyncio.sleep(_EXEC_RESET_DELAY)
            if self.fsm_state == "executing":
                self.fsm_state = "standing"

        self._exec_reset_task = asyncio.create_task(_reset())
