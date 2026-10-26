"""Goal-bounded selection from the lamp's own factual episodes."""

from cloud.esp32.agentcore.memory.episode import recent


def relevant(goal: str, context, limit: int = 5) -> list[dict]:
    records = recent(context, limit=50)
    terms = {goal[index:index + 2] for index in range(max(0, len(goal) - 1))}
    ranked = sorted(records, key=lambda entry: (
        sum(term in entry.get("detail", "") for term in terms), entry.get("ts", 0)
    ), reverse=True)
    return ranked[:max(1, min(limit, 10))]
