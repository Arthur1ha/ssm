"""Resolve short decision explanations against the current shared facts.

This is a projection of existing context, not another memory or device registry.
Models select references; the server supplies the evidence snapshots displayed
to people. An observation remains an observation, never a motion receipt.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any

from cloud.peers.view import STATE_MAX_AGE_S

MAX_REASON = 400
MAX_EVIDENCE = 8
_DISCOVERY_MAX_AGE_S = 600
_STATE_LABELS = {
    "connection_state": "连接", "activity_state": "活动状态",
    "connected": "设备连接",
    "life_state": "任务状态", "body_state": "身体姿态",
    "autonomy_mode": "自主模式", "control_mode": "控制模式",
    "ism": "灯光状态", "brightness": "亮度", "battery_soc": "电量",
    "r": "红", "g": "绿", "b": "蓝", "count": "闪烁次数", "level": "光线级别",
    "detected": "检测到信号", "presence": "有人", "value": "读数",
}
_STATE_VALUES = {
    "idle": "空闲", "rest": "休息", "active": "活动中",
    "play": "执行任务", "explore": "探索中", "standing": "站立",
    "lying": "趴卧", "sitting": "坐着", "online": "在线",
    "offline": "离线", "manual": "手动", "reactive": "自主响应",
    "free_explore": "自由探索", "OFF": "熄灭", "ON": "常亮",
    "BLINKING": "闪烁", "normal": "正常", "UNKNOWN": "未确认",
    "DARK": "暗", "DIM": "微亮", "NORMAL": "正常", "BRIGHT": "明亮",
}


def _fresh(value: Any, now: float, max_age: float) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and value > 0 and 0 <= now - value <= max_age)


def _state_text(state: dict) -> str:
    parts = []
    for key, label in _STATE_LABELS.items():
        value = state.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            value = "是" if value else "否"
        elif isinstance(value, str):
            value = _STATE_VALUES.get(value, value)
        elif not (type(value) in (int, float) and math.isfinite(value)):
            continue
        parts.append(f"{label}：{value}")
    return "；".join(parts)


def decision_evidence(space: dict, *, now: float | None = None) -> list[dict]:
    """Offer bounded references to actual facts, including their provenance."""
    instant = time.time() if now is None else now
    evidence: list[dict] = []

    def add(ref, kind, source, as_of, summary, *, status=None):
        if not isinstance(summary, str) or not summary.strip():
            return
        if kind in {"peer_state", "environment", "activity", "constraint"}:
            # The fingerprint identifies the observed values. Execution can
            # resolve this reference against the server's saved model-turn
            # context, without relabeling a newer reading as that observation.
            fingerprint = hashlib.sha256(summary.strip().encode()).hexdigest()[:16]
            ref = f"{ref}:{fingerprint}"
        item = {"ref": str(ref), "kind": kind, "source": str(source),
                "as_of": as_of, "summary": summary.strip()[:240]}
        if status is not None:
            item["status"] = status
        evidence.append(item)

    activity = space.get("activity") or {}
    activity_id = activity.get("activity_id")
    if activity_id:
        add(f"activity:{activity_id}", "activity", "user", activity.get("started_at"),
            str(activity.get("context") or "当前空间活动正在进行"))

    for peer in space.get("peers") or []:
        unit_id = peer.get("unit_id")
        state = peer.get("state_summary") or {}
        if not unit_id:
            continue
        if (state.get("status") == "current"
                and _fresh(state.get("as_of"), instant, STATE_MAX_AGE_S)):
            name = str(peer.get("name") or unit_id)
            online = "在线" if peer.get("online") else "离线"
            add(f"peer:{unit_id}", "peer_state", unit_id, state["as_of"],
                f"{name}{online}；{_state_text(state)}", status="current")

    for entry in space.get("environment") or []:
        source = entry.get("source")
        if (source and entry.get("status") == "current"
                and _fresh(entry.get("as_of"), instant, STATE_MAX_AGE_S)):
            add(f"environment:{source}", "environment", source, entry["as_of"],
                _state_text(entry.get("values") or {}), status="current")

    for requirement in space.get("constraints") or []:
        if not _fresh(requirement.get("issued_at"), instant, 3600):
            continue
        expires = requirement.get("expires_at")
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= instant:
            continue
        if requirement.get("constraint_id"):
            add(f"constraint:{requirement['constraint_id']}", "constraint", "user",
                requirement["issued_at"], requirement.get("text"))

    for discovery in space.get("recent_discoveries") or []:
        if (not activity_id or discovery.get("activity_id") != activity_id
                or not _fresh(discovery.get("ts"), instant, _DISCOVERY_MAX_AGE_S)):
            continue
        kind = discovery.get("event_type")
        if kind not in {"observation", "person_identified", "action_result", "message", "response"}:
            continue
        if kind == "action_result" and discovery.get("status") not in {
                "ok", "blocked", "rejected", "cancelled", "error"}:
            continue
        if discovery.get("event_id"):
            add(discovery["event_id"], kind,
                discovery.get("source_unit_id") or discovery.get("unit_id") or "unknown",
                discovery["ts"], discovery.get("summary"), status=discovery.get("status"))
    return evidence


def resolve_decision(space: dict, reason: str, refs: list[str], *,
                     require_observation: bool = False,
                     observed_space: dict | None = None) -> dict:
    """Reject missing/stale references; never trust model-written evidence text."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON:
        raise ValueError("decision_reason_required")
    if (not isinstance(refs, list) or not 1 <= len(refs) <= MAX_EVIDENCE
            or not all(isinstance(ref, str) and 0 < len(ref) <= 200 for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError("decision_evidence_required")
    instant = time.time()
    current_evidence = decision_evidence(space, now=instant)
    if observed_space is not None:
        # This snapshot is supplied by the trusted Graph, never by tool args.
        # Observations may remain valid across a sensor update, but a changed
        # activity or user requirement must trigger a fresh decision.
        activity_keys = ("activity_id", "revision", "context", "status")
        current_activity, observed_activity = space.get("activity") or {}, observed_space.get("activity") or {}
        if any(current_activity.get(key) != observed_activity.get(key) for key in activity_keys):
            raise ValueError("decision_context_changed")
        evidence = decision_evidence(observed_space, now=instant)
        if ({item["ref"] for item in current_evidence if item["kind"] == "constraint"}
                != {item["ref"] for item in evidence if item["kind"] == "constraint"}):
            raise ValueError("decision_context_changed")
    else:
        evidence = current_evidence
    catalog = {item["ref"]: item for item in evidence}
    if any(ref not in catalog for ref in refs):
        raise ValueError("decision_evidence_unavailable")
    selected = [catalog[ref] for ref in refs]
    if require_observation and not any(item["kind"] == "observation" for item in selected):
        raise ValueError("decision_observation_required")
    return {"reason": reason.strip(), "evidence": selected}
