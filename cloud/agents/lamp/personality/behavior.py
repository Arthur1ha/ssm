"""Lamp personality scores tune reactive evaluation opportunities."""


def settled_light_level(agent, observed: str, now: float, sensitivity: int) -> str:
    """Use an edge-confirmed light category only after it remains stable."""
    stable = getattr(agent, "_stable_light_level", "")
    if not stable:
        agent._stable_light_level = observed
        return observed
    if observed == stable or observed == "UNKNOWN":
        agent._pending_light_level = ""
        return stable
    if observed != getattr(agent, "_pending_light_level", ""):
        agent._pending_light_level = observed
        agent._pending_light_since = now
        return stable
    settle_seconds = max(6, 24 - max(0, min(100, sensitivity)) // 5)
    if now - getattr(agent, "_pending_light_since", now) < settle_seconds:
        return stable
    agent._stable_light_level = observed
    agent._pending_light_level = ""
    return observed
