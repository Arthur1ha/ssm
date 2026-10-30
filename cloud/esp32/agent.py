import queue
import time
import logging
from typing import Optional

from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

from cloud.esp32.state import ESP32State
from cloud.esp32.agentcore.llm import get_text_llm
from cloud.esp32.agentcore.mode.reactive import ReactiveMind
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
        from cloud.esp32.agentcore.soul import ensure_personality_profile
        ensure_personality_profile()
        self._state = state
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
        self._user_hold_until: float = 0.0
        self._last_reason_sig: tuple | None = None   # 上次问 LLM 时的情境签名（实质变化门）
        self._last_reason_ts: float = 0.0
        from cloud.peers.social import SocialState
        self._social_state = SocialState()

    def get_autonomy_mode(self) -> str:
        """返回当前自主模式：manual（仅听命令）/ reactive（自发调光）。"""
        return self._autonomy_mode

    def set_autonomy_mode(self, mode: str) -> None:
        """切换自主模式；非法值抛 ValueError。"""
        if mode not in _VALID_AUTONOMY_MODES:
            raise ValueError(f"未知自主模式: {mode}")
        self._autonomy_mode = mode
        if mode == "manual":
            from cloud.peers.gateway import cancel_pending
            cancel_pending(initiator_unit_id=self._find_led_unit(),
                           origin_kind="agent_autonomous", reason="mode_changed")
        if mode == "reactive":
            self._last_reason_sig = None   # 切回自主时清签名，立刻按当前情境评估一次
        else:
            self._user_hold_until = 0.0
        logger.info("autonomy mode → %s", mode)

    def mark_user_command(self, unit_id: str) -> None:
        """自主模式收到外部命令时，开启让位窗口。"""
        if self._autonomy_mode != "reactive":
            return
        self._user_hold_until = time.time() + USER_HOLD_TTL
        from cloud.peers.gateway import cancel_pending
        cancel_pending(initiator_unit_id=unit_id, origin_kind="agent_autonomous",
                       reason="user_takeover")
        logger.info("user command on %s → 自主层让位 %ds", unit_id, USER_HOLD_TTL)

    def mark_peer_command(self, unit_id: str) -> None:
        """Peer requests get a shorter reactive hold without cancelling peer work."""
        if self._autonomy_mode != "reactive":
            return
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
                      deadline: float | None = None) -> dict:
        """Run the active lamp conversation loop."""
        from cloud.esp32.multitask import run_lamp_loop
        return run_lamp_loop(self, text, unit_id, context, dispatch,
                             lighting_only=lighting_only, run_id=run_id,
                             resume=resume, trace_id=trace_id,
                             parent_run_id=parent_run_id, deadline=deadline)

    def _make_llm(self):
        return get_text_llm(ChatOpenAI)
