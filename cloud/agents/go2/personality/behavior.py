"""Go2 personality scores select existing opportunities, never motion permissions."""


def exploration_interval(curiosity: int) -> int:
    """Seconds between idle frontier evaluations (70 keeps the old 10s cadence)."""
    return max(7, min(17, 17 - curiosity // 10))


def social_action_candidates(boldness: int, available: set[str]) -> set[str]:
    """A cautious Go2 observes first and limits optional gestures to Hello."""
    if boldness < 30:
        return available.intersection({"Hello"})
    return set(available)


def reactive_action_candidates(boldness: int, postures: set[str], turns: set[str]) -> set[str]:
    """Offer existing reactive gestures and turns by preference only."""
    if boldness < 30:
        return postures.intersection({"Hello"})
    if boldness < 60:
        return set(postures)
    return set(postures) | set(turns)


def decision_persona(profile: dict) -> str:
    traits = profile["traits"]
    return (
        f"{profile['prompt']}\n"
        f"好奇心 {traits['curiosity']}/100 只影响空闲探索评估机会；"
        f"外向性 {traits['extraversion']}/100 影响有人互动时表达和同伴交流机会；"
        f"大胆程度 {traits['boldness']}/100 只影响已允许的互动候选偏好。"
        "这些数值不授予运动权限，也不能覆盖地图、避障和用户停止。"
    )


def reply_persona(profile: dict) -> str:
    """Give the reply model an observable speaking preference from extraversion."""
    extraversion = profile["traits"]["extraversion"]
    if extraversion < 30:
        expression = "保留简单短句和一点童稚感，主动开口较少，不主动延长闲聊。"
    elif extraversion >= 70:
        expression = "表达活泼、热情，带一点童稚的惊喜；适合互动时可自然接话或问一个相关问题。"
    else:
        expression = "用简单短句友善回应，按用户投入的程度接话，不过度热络。"
    return f"{profile['prompt']}\n外向性 {extraversion}/100：{expression}"
