"""Finite single-hop coordination policy for public device skills."""


def allows_step(task: dict, skill: dict, kind: str, origin_kind: str) -> bool:
    delegated = origin_kind == "user_delegated"
    params = task.get("params") or {}
    if (kind == "mqtt" and "light" in skill.get("tags", [])
            and (skill.get("invoke") or {}).get("action") == "SET_STATE"):
        return params.get("state") in {"ON", "OFF"}
    if task.get("skill_id") == "blink_light" and kind == "mqtt":
        return delegated
    if task.get("skill_id") == "adjust_lighting" and kind == "http":
        return delegated and isinstance(params.get("goal"), str) and bool(params["goal"].strip())
    if (task.get("unit_id") == "go2" and task.get("skill_id") == "go2_sport"
            and kind == "http"):
        return params.get("cmd") in ({"Hello", "Dance1", "Dance2"} if delegated else {"Hello"})
    if task.get("unit_id") == "go2" and task.get("skill_id") == "go2_move" and kind == "http":
        duration = params.get("duration")
        speed = params.get("speed", 0.3)
        return (delegated
                and params.get("direction") in {"forward", "backward", "left", "right", "turn_left", "turn_right"}
                and type(duration) in {int, float} and 0 < duration <= 10
                and type(speed) in {int, float} and 0 < speed <= 0.5)
    return False
