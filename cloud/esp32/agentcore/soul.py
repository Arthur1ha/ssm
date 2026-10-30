"""Compatibility facade for lamp personality callers."""

import hashlib

from cloud.esp32.agentcore.personality import profile
from cloud.observability import emit_agent_event

AGENT_PERSONA = profile.DEFAULT_PROMPT


def ensure_personality_profile() -> None:
    profile.get_profile()


def get_personality_view() -> dict:
    current = profile.get_profile()
    return {"prompt": current["prompt"],
            "social_initiative": current["traits"]["social_initiative"]}


def load_personality() -> tuple[str, str, str | None]:
    prompt = profile.get_profile()["prompt"]
    return prompt, "default" if prompt == AGENT_PERSONA else "custom", None


def get_personality() -> str:
    return profile.get_profile()["prompt"]


def set_personality(prompt: str, context, social_initiative: int | None = None) -> None:
    if (getattr(context, "actor", None) != "admin"
            or getattr(context, "persist", None) is not True
            or getattr(context, "origin_kind", None) != "user_delegated"):
        raise PermissionError("administrator persistence required")
    previous = get_personality()
    changes = {"prompt": prompt, "revision": profile.get_profile()["revision"]}
    if social_initiative is not None:
        changes["traits"] = {"social_initiative": social_initiative}
    profile.update_profile(changes, context)
    emit_agent_event("persona.changed", agent_id="esp32_desk_led",
                     operator_kind="admin",
                     before_hash=hashlib.sha256(previous.encode()).hexdigest(),
                     after_hash=hashlib.sha256(prompt.encode()).hexdigest())
