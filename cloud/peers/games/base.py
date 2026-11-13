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


