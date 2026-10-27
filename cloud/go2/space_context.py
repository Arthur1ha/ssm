"""Go2's bounded view of the shared MQTT space snapshot."""

import json

from cloud.space.context import get_space_context


def prompt_space_context() -> str:
    from cloud.api.main import _esp32_state
    context = get_space_context("go2", state=_esp32_state)
    context["peers"] = [
        {"unit_id": peer.get("unit_id"), "name": str(peer.get("name") or "")[:80],
         "online": peer.get("online"), "state_summary": peer.get("state_summary"),
         "skills": [{"id": skill.get("id"), "name": str(skill.get("name") or "")[:80],
                     "tags": (skill.get("tags") or [])[:5]}
                    for skill in (peer.get("skills") or [])[:5]]}
        for peer in context["peers"][:8]]
    context["environment"] = context["environment"][:12]
    context["constraints"] = context["constraints"][:8]
    context["recent_discoveries"] = context["recent_discoveries"][-6:]
    return json.dumps(context, ensure_ascii=False, default=str)
