"""Go2 personality scores select existing opportunities, never motion permissions."""

from cloud.personality.store import render_identity


def exploration_interval(curiosity: int) -> int:
    """Seconds between idle frontier evaluations (70 keeps the old 10s cadence)."""
    return max(7, min(17, 17 - curiosity // 10))

def reply_persona(profile: dict) -> str:
    """Give the reply model an observable speaking preference from extraversion."""
    extraversion = profile["traits"]["extraversion"]
    if extraversion < 30:
        expression = "按当前人格自然回应，主动开口较少，不主动延长闲聊。"
    elif extraversion >= 70:
        expression = "按当前人格更主动地表达和分享；适合互动时可自然接话，不必每次追问。"
    else:
        expression = "按当前人格自然回应，按用户投入的程度接话，不过度热络。"
    return f"{render_identity(profile)}\n外向性 {extraversion}/100：{expression}"
