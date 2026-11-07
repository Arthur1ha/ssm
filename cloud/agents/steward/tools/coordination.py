"""Transport adapter for the shared, finite collaboration policy."""

from cloud.peers.policy import permits_skill


def allows_step(task: dict, skill: dict, kind: str, origin_kind: str) -> bool:
    target = task.get("unit_id", "")
    skill_id = task.get("skill_id", "")
    # Preserve the generic lamp SET_STATE binding on older Cards.
    if (kind == "mqtt" and "light" in skill.get("tags", [])
            and (skill.get("invoke") or {}).get("action") == "SET_STATE"):
        skill_id = "set_light_state"
    expected = "http" if target == "go2" or skill_id == "adjust_lighting" else "mqtt"
    return kind == expected and permits_skill(
        target, skill_id, task.get("params") or {}, origin_kind, audience="steward")
