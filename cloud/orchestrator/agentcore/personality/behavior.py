"""Coordinator warmth and genuine-ambiguity preferences."""


def warmth_guidance(warmth: int) -> str:
    if warmth >= 70:
        return "保持沉稳，语气可以更亲切；合适时简短表达关照，不增加未经确认的承诺。"
    if warmth <= 30:
        return "语气沉稳克制，少用寒暄，不改变真实结果。"
    return "语气沉稳友善，简短回应，不改变真实结果。"


def clarification_guidance(tendency: int) -> str:
    if tendency >= 70:
        return "当意图或必要参数确有歧义时，优先追问一个具体问题；明确请求仍继续规划。"
    if tendency <= 30:
        return "当意图或必要参数确有歧义、但已有可靠上下文足以确定时，继续规划；否则追问。"
    return "仅在意图或必要参数确有歧义时追问；不要为了风格偏好反复确认明确请求。"


def current_persona(profile: dict | None = None) -> str:
    """Render the saved steward identity with its current speaking preference."""
    from cloud.orchestrator.agentcore.personality.profile import get_profile
    profile = profile or get_profile()
    warmth = profile["traits"]["warmth"]
    return f"{profile['prompt']}\n温暖程度 {warmth}/100，{warmth_guidance(warmth)}"
