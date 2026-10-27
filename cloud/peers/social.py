"""Per-agent, activity-scoped opportunities and limits for voluntary messages."""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from cloud.space.activity import get_activity_store

_MIN_SEND_GAP = 300
_WINDOW = 1800
_MAX_SENDS = 3


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
        self.sent_times: deque[float] = deque()
        self.seen_discoveries: set[str] = set()
        self.topic_times: dict[tuple[str, str], float] = {}
        self._joined_pending = False

    def reset(self, activity_id: str | None, discoveries: list[dict] = (),
              *, joined_at: float = 0.0) -> None:
        self.activity_id = activity_id
        self.last_evaluated = 0.0
        self.last_sent = 0.0
        self.sent_times.clear()
        self.topic_times.clear()
        self.seen_discoveries = {item["event_id"] for item in discoveries if item.get("event_id")}
        self._joined_pending = bool(activity_id and joined_at >= self._born_at - 1)

    def opportunity(self, activity: dict | None, peers: list[dict], discoveries: list[dict],
                    score: int = 50, *, unit_id: str = "", now: float | None = None) -> dict | None:
        instant = time.time() if now is None else now
        if type(score) is not int or not 0 <= score <= 100:
            score = 50
        activity_id = activity.get("activity_id") if activity and peers else None
        if activity_id != self.activity_id:
            member = (activity.get("devices") or {}).get(unit_id, {}) if activity else {}
            # Old snapshots may lack joined_at. In that case only a newly
            # started activity qualifies, so restarting never replays a join.
            joined_at = (member.get("joined_at") or activity.get("started_at") or 0
                         if activity else 0)
            self.reset(activity_id, discoveries, joined_at=float(joined_at))
        if not activity_id:
            return None
        relevant = [row for row in discoveries if row.get("event_type") in
                    {"observation", "person_identified", "action_result"}
                    and not row.get("message_id") and row.get("event_id") not in self.seen_discoveries]
        if relevant:
            return {"kind": "discovery", "event_ids": [row["event_id"] for row in relevant],
                    "discoveries": relevant}
        if self._joined_pending and score >= 35:
            return {"kind": "joined", "event_ids": [], "discoveries": []}
        interval = 300 if score >= 70 else 900 if score >= 35 else None
        if interval and self.last_evaluated and instant - self.last_evaluated >= interval:
            return {"kind": "quiet", "event_ids": [], "discoveries": []}
        return None

    def evaluated(self, opportunity: dict, *, now: float | None = None) -> None:
        self.last_evaluated = time.time() if now is None else now
        self._joined_pending = False
        self.seen_discoveries.update(opportunity.get("event_ids") or [])
        if len(self.seen_discoveries) > 100:
            self.seen_discoveries = set(list(self.seen_discoveries)[-100:])

    def can_send(self, target: str, topic: str, *, now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        while self.sent_times and self.sent_times[0] <= instant - _WINDOW:
            self.sent_times.popleft()
        key = (target, topic.strip().lower()[:100])
        last_topic = self.topic_times.get(key)
        return (bool(self.activity_id) and (not self.last_sent or instant - self.last_sent >= _MIN_SEND_GAP)
                and len(self.sent_times) < _MAX_SENDS
                and (last_topic is None or instant - last_topic >= _WINDOW))

    def sent(self, target: str, topic: str, *, now: float | None = None) -> None:
        instant = time.time() if now is None else now
        self.last_sent = instant
        self.sent_times.append(instant)
        self.topic_times[(target, topic.strip().lower()[:100])] = instant
