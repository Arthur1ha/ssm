"""Shared peer game contract and activity membership checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


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
    if (state.get("connected") is not True or state.get("connection_state") != "online"
            or state.get("activity_state") != "active"
            or state.get("life_state") not in {"idle", "explore"}
            or state.get("life_task_id") is not None
            or state.get("pending_action") is not None
            or state.get("navigation_active") is not False
            or state.get("velocity_requested") is not False
            or state.get("control_mode") != "normal"
            or state.get("body_state") == "moving"
            or drive.user_interrupt or drive._pending_question_id or drive._active_goal):
        return "go2_busy"
    lease = control_lease.snapshot()
    # A completed autonomous exploration segment may retain its own lease.
    # An outstanding user/other session's lease still reserves control.
    if lease is not None and lease.owner_session_id != state.get("exploration_owner"):
        return "go2_motion_owned_elsewhere"
    return None


