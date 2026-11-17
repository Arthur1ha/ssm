"""Shared peer game contract and activity membership checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


GAME_CONTEXT_GUIDANCE = (
    "game_catalog（games）是已注册游戏目录，保留暂时不能开局的玩法与规则；"
    "ready=false 或当前可玩列表为空不表示没有这些游戏。"
    "只邀请 ready=true 的玩法，发送和接受仍须通过实时门禁。"
    "阻塞原因以 reason 为准：go2_remote_control 表示仍处于遥控模式，"
    "不能据此断言用户正在操纵或机器狗正在走动；"
    "go2_action_unconfirmed 表示动作回执未确认，go2_navigation_active 表示导航未结束，"
    "go2_not_stopped 表示运动请求或移动状态尚未解除，"
    "go2_task_active 表示任务仍占用，go2_waiting_for_answer 表示等待回答。"
    "life_state=explore 本身不表示正在移动或没有空参与游戏，轮次间可以邀请。"
    "只陈述已提供的具体阻碍，其他原因不猜测；不能把暂时受阻说成游戏未注册。"
)


@dataclass(frozen=True)
class Game:
    game_id: str
    name: str
    description: str
    participants: tuple[str, str]
    available: Callable[[str, str], bool]
    execute: Callable[[object, dict, str], Awaitable[None]]
    invitation: str = ""
    rules: str = ""
    strategy: str = ""
    valid_invitation: Callable[[str], bool] | None = None
    on_acceptance: Callable[[object, dict, object], None] | None = None
    acceptance: str = "我接受，一起玩吧。"
    bidirectional: bool = False
    requires_idle: bool = False
    on_invitation: Callable[[object, dict, object], None] | None = None
    invitation_ttl_s: int = 300

    def allows_pair(self, sender: str, target: str) -> bool:
        pair = (sender, target)
        return pair == self.participants or (
            self.bidirectional and pair == self.participants[::-1])

    def metadata(self) -> dict:
        """Public rules and guidance supplied to the existing role Graphs."""
        return {"game_id": self.game_id, "name": self.name,
                "description": self.description, "rules": self.rules,
                "strategy": self.strategy}


def activity_allows(activity_id: str | None, sender: str, target: str) -> bool:
    from cloud.space.activity import get_activity_store

    activity = get_activity_store().current()
    members = (activity or {}).get("devices") or {}
    return bool(activity_id and activity and activity.get("activity_id") == activity_id
                and activity.get("status") in {"starting", "active", "partial"}
                and members.get(sender, {}).get("status") == "joined"
                and members.get(target, {}).get("status") == "joined")


def game_invitation_ready(activity_id: str | None, sender: str, target: str) -> bool:
    return game_invitation_block_reason(activity_id, sender, target) is None


def game_invitation_block_reason(activity_id: str | None, sender: str, target: str) -> str | None:
    """Check actual owners' task facts before inviting/accepting, not in-play.

    Both device services live in the API process. Missing live services fail
    closed; importing an uninitialized singleton in another process is not
    evidence of an idle device. This creates no second device state store.
    """
    if not activity_allows(activity_id, sender, target):
        return "activity_not_joined"
    from cloud.esp32.service import get_agent
    from cloud.go2.connection import go2
    from cloud.go2.control import control_lease
    from cloud.go2.mode.free_explore import drive

    lamp = get_agent()
    if lamp is None:
        return "lamp_agent_unavailable"
    if not lamp._should_act():
        return "lamp_autonomy_inactive"
    if not lamp._device_online():
        return "lamp_offline"
    if (getattr(lamp, "_state", None) is not None
            and lamp._state.has_pending_tasks()):
        return "lamp_result_unconfirmed"
    if lamp.is_busy_for_game():
        return "lamp_busy"
    if not go2.is_connected or getattr(go2, "runtime", None) is None:
        return "go2_disconnected"
    state = go2.runtime.snapshot()
    if state.get("connected") is not True or state.get("connection_state") != "online":
        return "go2_disconnected"
    if state.get("control_mode") != "normal":
        return "go2_remote_control" if state.get("control_mode") == "remote" else "go2_control_unknown"
    if state.get("pending_action") is not None:
        return "go2_action_unconfirmed"
    if state.get("navigation_active") is not False:
        return "go2_navigation_active"
    if state.get("velocity_requested") is not False or state.get("body_state") == "moving":
        return "go2_not_stopped"
    if state.get("activity_state") != "active":
        return "go2_inactive"
    if drive.user_interrupt:
        return "go2_interrupted"
    if drive._pending_question_id:
        return "go2_waiting_for_answer"
    if (state.get("life_task_id") is not None or drive._active_goal
            or state.get("life_state") not in {"idle", "explore"}):
        return "go2_task_active"
    lease = control_lease.snapshot()
    # A completed autonomous exploration segment may retain its own lease.
    # An outstanding user/other session's lease still reserves control.
    if lease is not None and lease.owner_session_id != state.get("exploration_owner"):
        return "go2_motion_owned_elsewhere"
    return None


