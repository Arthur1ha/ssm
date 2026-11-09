"""Bounded dialogue in the existing LangGraph agent/session thread."""

from langchain_core.messages import AIMessage, HumanMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately


def trim_dialogue(messages):
    """Keep complete recent exchanges, within both a turn and token budget."""
    recent = trim_messages(messages, max_tokens=16, token_counter=len,
                           strategy="last", start_on="human", end_on="ai")
    return trim_messages(recent, max_tokens=3000,
                         token_counter=count_tokens_approximately,
                         strategy="last", start_on="human", end_on="ai")


def dialogue_turn(run):
    answer = run.response or run.decision_draft
    if not answer:
        return []
    text = run.goal
    if run.supplement:
        text += "\n用户补充：" + run.supplement
    return [HumanMessage(content=text, id=f"{run.run_id}:user"),
            AIMessage(content=answer, id=f"{run.run_id}:assistant")]


def legacy_dialogue(saver, config, current_run_id):
    """Seed the message channel once for sessions created before it existed."""
    turns = []
    seen = {current_run_id}
    for checkpoint in saver.list(config, limit=120):
        data = checkpoint.checkpoint.get("channel_values", {}).get("run")
        if (not isinstance(data, dict) or not data.get("run_id")
                or data["run_id"] in seen
                or data.get("status") in {"executing", "needs_clarification"}
                or not data.get("reply_ready", True)):
            continue
        seen.add(data["run_id"])
        answer = data.get("response") or data.get("decision_draft")
        if not data.get("goal") or not answer:
            continue
        text = data["goal"]
        if data.get("supplement"):
            text += "\n用户补充：" + data["supplement"]
        turns.append([HumanMessage(content=text, id=f"{data['run_id']}:user"),
                      AIMessage(content=answer, id=f"{data['run_id']}:assistant")])
        if len(turns) >= 8:
            break
    return trim_dialogue([message for turn in reversed(turns) for message in turn])
