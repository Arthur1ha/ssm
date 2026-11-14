"""Per-agent, activity-scoped discovery deduplication for voluntary messages."""

from __future__ import annotations

import time
from typing import Any

from cloud.space.activity import get_activity_store


def activity_peers(unit_id: str, peers: list[dict[str, Any]]) -> tuple[dict | None, list[dict]]:
    activity = get_activity_store().current()
    if (not activity or activity.get("status") not in {"starting", "active", "partial"}
            or (activity.get("devices") or {}).get(unit_id, {}).get("status") != "joined"):
        return None, []
    joined = {name for name, status in (activity.get("devices") or {}).items()
              if status.get("status") == "joined"}
    return activity, [peer for peer in peers if peer.get("unit_id") != unit_id
                      and peer.get("unit_id") in joined and peer.get("online")]


class SocialState:
    def __init__(self) -> None:
        self._born_at = time.time()
        self.activity_id: str | None = None
        self.last_evaluated = 0.0
        self.last_sent = 0.0
        self.seen_discoveries: set[str] = set()
        self._idle_ready = False
        self._idle_episode = 0

    def reset(self, activity_id: str | None, discoveries: list[dict] = ()) -> None:
        self.activity_id = activity_id
        self.last_evaluated = 0.0
        self.last_sent = 0.0
        self._idle_ready = False
        self._idle_episode = 0
        # Ignore observations predating this process, but keep new discoveries
        # made before this agent's first evaluation of the activity.
        self.seen_discoveries = {item["event_id"] for item in discoveries
                                 if item.get("event_id") and item.get("ts", 0) < self._born_at}

    def opportunity(self, activity: dict | None, peers: list[dict], discoveries: list[dict],
                    score: int = 50, *, unit_id: str = "", now: float | None = None,
                    idle_ready: bool = False) -> dict | None:
        instant = time.time() if now is None else now
        if type(score) is not int or not 0 <= score <= 100:
            score = 50
        activity_id = activity.get("activity_id") if activity and peers else None
        if activity_id != self.activity_id:
            self.reset(activity_id, discoveries)
        if not activity_id:
            return None
        if self._idle_ready and not idle_ready:
            self._idle_episode += 1
        self._idle_ready = idle_ready
        idle_id = f"idle:{activity_id}:{self._idle_episode}"
        relevant = [row for row in discoveries if row.get("event_type") in
                    {"observation", "person_identified", "action_result"}
                    and not row.get("message_id") and row.get("event_id") not in self.seen_discoveries
                    and row.get("activity_id") == activity_id
                    and isinstance(row.get("ts"), (int, float))
                    and max(self._born_at, activity.get("started_at", 0)) <= row["ts"] <= instant
                    and instant - row["ts"] <= 600]
        if relevant:
            return {"kind": "discovery", "event_ids": [row["event_id"] for row in relevant]
                    + ([idle_id] if idle_ready else []),
                    "discoveries": relevant}
        if idle_ready and idle_id not in self.seen_discoveries:
            # This is a scheduling opportunity, not an observation/evidence
            # reference. The Graph can invite, talk, or choose silence.
            return {"kind": "idle", "event_ids": [idle_id],
                    "summary": "双方当前没有正在执行的任务，可以自主判断是否交流或邀请。"}
        return None

    def evaluated(self, opportunity: dict, *, now: float | None = None) -> None:
        self.last_evaluated = time.time() if now is None else now
        self.seen_discoveries.update(opportunity.get("event_ids") or [])
        if len(self.seen_discoveries) > 100:
            self.seen_discoveries = set(list(self.seen_discoveries)[-100:])

    def can_send(self, target: str, topic: str, *, now: float | None = None) -> bool:
        """Keep the activity gate; callers recheck live peers and task availability.

        Timing and topic repetition are context for the agent, not send quotas.
        The arguments remain compatible with the existing dispatch adapters.
        """
        return bool(self.activity_id)

    def sent(self, target: str, topic: str, *, now: float | None = None) -> None:
        instant = time.time() if now is None else now
        self.last_sent = instant
