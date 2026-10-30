"""Skill persistence and transport timeouts for the active coordinator loop."""

_PERSISTENT_SKILL_MARKERS = (
    "define_rule", "create_rule", "update_rule", "delete_rule",
    "save_location", "tag_location", "delete_location", "set_home",
    "personality", "soul", "training", "train_", "tune_",
    "write_memory", "save_memory", "add_episode", "daily_summary",
    "enroll_face", "create_person", "update_person", "delete_person",
    "list_people",
)


def skill_requires_persistence(skill: dict) -> bool:
    if skill.get("persistent") is True or skill.get("access") == "admin":
        return True
    tags = set(skill.get("tags", []))
    if tags.intersection({"persistent", "admin", "write", "configuration"}):
        return True
    invoke = skill.get("invoke", {})
    value = " ".join((str(skill.get("id", "")), str(invoke.get("action", "")))).lower()
    return any(marker in value for marker in _PERSISTENT_SKILL_MARKERS)


def http_timeout_for(card: dict, skill_id: str) -> float:
    """Give long-running Go2 and semantic lamp skills their full task budget."""
    if card.get("unit_id") == "go2":
        return 95
    if skill_id == "adjust_lighting":
        return 60
    for skill in card.get("skills", []):
        if skill.get("id") == skill_id:
            if "navigation" in skill.get("tags", []):
                return 30
            break
    return 10
