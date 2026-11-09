import queue
import time
import logging
import threading
import math
from typing import Optional

logger = logging.getLogger(__name__)

from cloud.esp32.state import ESP32State
from cloud.agents.common.models import get_text_model
from cloud.agents.lamp.workflows.runtime import ReactiveMind
from cloud.space.execution import ExecutionContext

USER_HOLD_TTL = 300   # 用户/编排器命令后，自主层让位时长（秒）
PEER_HOLD_TTL = 60    # 同伴控灯请求执行期间，避免自主调光覆盖其结果
_VALID_AUTONOMY_MODES = ("manual", "reactive")

_agent: Optional["ESP32Agent"] = None


def init(state: ESP32State, llm=None) -> "ESP32Agent":
    global _agent
    _agent = ESP32Agent(state, llm=llm)
    return _agent


def get_agent() -> Optional["ESP32Agent"]:
    return _agent


class ESP32Agent(ReactiveMind):
    def __init__(self, state: ESP32State, llm=None):
        from cloud.agents.lamp.personality.profile import get_profile
        get_profile()
        self._state = state
        self._llm_injected = llm is not None
        self._llm   = llm if llm is not None else self._make_llm()
        self._event_queue: queue.Queue = queue.Queue()
        self._belief_history: list[dict] = []
        self._cooldown: dict[str, float] = {}
        self._last_sound_ts: float = 0.0
        self._work_start_ts: float = 0.0
        self._last_act_ts: float = 0.0
        self._last_light_level: str = ""
        self._last_proactive_ts: float = 0.0
        self._belief_summary: str = ""
        self._beliefs_since_summary: int = 0
        self._autonomy_mode: str = "manual"   # 默认手动：连上不自动控灯，reactive 需显式开启
        self._autonomy_epoch = 0
        self._command_epoch = 0
        self._autonomy_since = 0.0
        self._user_hold_until: float = 0.0
        self._control_view_lock = threading.Lock()
        self._control_view_signature = None
        self._control_view_ts = 0.0
        self._last_reason_sig: tuple | None = None   # 上次问 LLM 时的情境签名（实质变化门）
        self._last_reason_ts: float = 0.0
        self._stable_light_level = ""
        self._pending_light_level = ""
        self._pending_light_since = 0.0
        from cloud.agents.common.autonomous_visibility import AutonomousReplyGate
        self._reply_gate = AutonomousReplyGate()
        from cloud.peers.social import SocialState
        self._social_state = SocialState()

    def get_autonomy_mode(self) -> str:
        """返回当前自主模式：manual（仅听命令）/ reactive（自发调光）。"""
        return self._autonomy_mode

    def control_snapshot(self) -> dict:
        """Project cloud control facts without changing the edge lighting state."""
        with self._control_view_lock:
            mode = self._autonomy_mode
            holding = mode == "reactive" and time.time() < self._user_hold_until
            state = "holding" if holding else "ready" if mode == "reactive" else "manual"
            hold_until = self._user_hold_until if holding else None
            signature = (mode, state, hold_until)
            if signature != self._control_view_signature:
                self._control_view_signature = signature
                self._control_view_ts = max(time.time(), math.nextafter(self._control_view_ts, math.inf))
            return {"autonomy_mode": mode, "control_state": state,
                    "control_hold_until": hold_until, "control_ts": self._control_view_ts}

    def set_autonomy_mode(self, mode: str) -> None:
        """切换自主模式；非法值抛 ValueError。"""
        if mode not in _VALID_AUTONOMY_MODES:
            raise ValueError(f"未知自主模式: {mode}")
        with self._control_view_lock:
            self._command_epoch += 1
        self._autonomy_epoch += 1
        self._autonomy_since = time.time()
        self._autonomy_mode = mode
        if mode == "manual":
            from cloud.peers.gateway import cancel_pending
            cancel_pending(initiator_unit_id=self._find_led_unit(),
                           origin_kind="agent_autonomous", reason="mode_changed")
        if mode == "reactive":
            self._last_reason_sig = None   # 切回自主时清签名，立刻按当前情境评估一次
            baseline = self._sense()
            self._autonomy_light_missing = not (baseline or {}).get("light_available", False)
            self._stable_light_level = (baseline or {}).get("light_level", "UNKNOWN")
            self._last_light_level = self._stable_light_level
            self._pending_light_level = ""
            self._work_start_ts = 0.0
            self._last_act_ts = 0.0
            self._reply_gate.reset()
        else:
            self._user_hold_until = 0.0
        logger.info("autonomy mode → %s", mode)

    def mark_user_command(self, unit_id: str) -> None:
        """Invalidate older behaviors; reactive mode also yields temporarily."""
        with self._control_view_lock:
            self._command_epoch += 1
        if self._autonomy_mode != "reactive":
            return
        self._autonomy_epoch += 1
        self._user_hold_until = time.time() + USER_HOLD_TTL
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id=unit_id, origin_kind="agent_autonomous",
                       reason="user_takeover")
        logger.info("user command on %s → 自主层让位 %ds", unit_id, USER_HOLD_TTL)

    def mark_peer_command(self, unit_id: str) -> None:
        """Peer requests get a shorter reactive hold without cancelling peer work."""
        with self._control_view_lock:
            self._command_epoch += 1
        if self._autonomy_mode != "reactive":
            return
        self._autonomy_epoch += 1
        self._user_hold_until = max(self._user_hold_until, time.time() + PEER_HOLD_TTL)
        logger.info("peer command on %s → 自主调光让位 %ds", unit_id, PEER_HOLD_TTL)

    def wait_task_result(self, task_id: str, timeout: float = 20.0) -> dict:
        """供设备页对话等待 ESP32 的实际执行反馈。"""
        return self._state.wait_task_result(task_id, timeout=timeout)

    def run_user_loop(self, text: str, unit_id: str, context: ExecutionContext,
                      dispatch, *, lighting_only: bool = False,
                      run_id: str | None = None, resume: bool = False,
                      trace_id: str | None = None,
                      parent_run_id: str | None = None,
                      deadline: float | None = None, selection: dict | None = None) -> dict:
        """Run the active lamp conversation loop."""
        from cloud.agents.lamp.graph import run_lamp_loop
        return run_lamp_loop(self, text, unit_id, context, dispatch,
                             lighting_only=lighting_only, run_id=run_id,
                             resume=resume, trace_id=trace_id,
                             parent_run_id=parent_run_id, deadline=deadline, selection=selection)

    def _make_llm(self):
        return get_text_model("lamp")

    def _llm_for_run(self):
        """Build inside async_model_clients so HTTP belongs to this run's loop."""
        return self._llm if self._llm_injected else self._make_llm()
