"""Bound execution summaries using the existing shared reply composer."""

import re

from cloud.agents.common.replies import (
    factual_fallback,
    finalize_reply,
    step_kind,
    valid_reply_format,
)


def valid_steward_reply(value, run) -> bool:
    if not valid_reply_format(value, run):
        return False
    # State queries and ordinary conversation may need a detailed explanation.
    # Only the closing text of a run that changed something gets this budget.
    if not any(step_kind(step) != "query" for step in run.steps):
        return True
    return len(value.strip()) <= 180 and len(re.findall(r"[。！？!?]+", value)) <= 2


def _fallback(run):
    # The shared action fallback cannot infer physical success from an activity.
    # Keep a confirmed activity start even when its invitation later failed.
    if any(step.get("tool") == "start_activity"
           and (step.get("result") or {}).get("result") == "ok" for step in run.steps):
        return "活动已启动，后续协作结果尚未确认。"
    return factual_fallback(run.agent_id, run.status, run)


async def finalize_steward_reply(run, *, model, instructions):
    if run.decision_draft and not valid_steward_reply(run.decision_draft, run):
        # An invalid final draft is already in the trace. Do not feed it back
        # as runtime_note: the existing composer should see this run's facts,
        # without the draft's unrelated historical topic or long explanation.
        run.decision_draft = ""
        run.response = ""
    return await finalize_reply(run, model=model, instructions=instructions,
                                validate=valid_steward_reply, fallback=_fallback)
