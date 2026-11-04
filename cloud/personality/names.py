"""Current user-facing agent names, keyed by stable unit ID."""


def agent_names() -> dict[str, str]:
    from cloud.agents.steward.personality.profile import get_profile as steward_profile
    from cloud.agents.lamp.personality.profile import get_profile as lamp_profile
    from cloud.agents.go2.personality.profile import get_profile as go2_profile

    return {profile["agent_id"]: profile["name"]
            for profile in (steward_profile(), lamp_profile(), go2_profile())}
