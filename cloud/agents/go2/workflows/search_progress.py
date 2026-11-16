"""Public search observations and executor receipts, without phase narration."""


def _text(value, limit=240):
    return value.strip()[:limit] if isinstance(value, str) else ""


def search_progress_facts(stage, state, *, observation=None, candidate=None, receipt=None):
    observation = observation if observation is not None else state.get("observation") or {}
    visual = observation.get("observation") or {}
    candidate = candidate or state.get("candidate") or {}
    result = receipt if receipt is not None else state.get("result") or {}
    is_observation = stage in {"search_observe", "search_candidate", "search_confirmed"}
    is_result = stage in {"search_move_result", "search_finished"}
    is_call = stage in {"search_observing", "search_rechecking", "search_navigate", "search_approach"}
    facts = {"text": "", "result_kind": "query" if is_observation else "action"}
    if is_observation:
        # Unknown is not an observation of absence. Preserve the actual frame
        # and its original timestamp rather than stamping a later UI event.
        seen = observation.get("found")
        facts.update(fact_kind="observation", text=_text(visual.get("scene_summary")),
            observation={"found": seen if type(seen) is bool else None,
                "frame_id": observation.get("frame_id"),
                "as_of": observation.get("as_of") or observation.get("ts"),
                "scene_summary": _text(visual.get("scene_summary")),
                "proximity_reason": _text(visual.get("proximity_reason"), 160),
                "proximity": visual.get("proximity") if visual.get("proximity") in
                    {"nearby", "far", "uncertain"} else "uncertain"})
    elif stage == "search_selected":
        # A deterministic planner's choice is execution feedback, never a
        # reconstructed explanation of what the role model was thinking.
        facts.update(fact_kind="decision", public_result={"candidate": {
            key: candidate[key] for key in ("source", "view_kind", "x", "y", "heading")
            if key in candidate}, "result_kind": "query"})
    elif is_call:
        facts.update(fact_kind="call", tool=("go2_observe" if stage in {
            "search_observing", "search_rechecking"} else "go2_navigate_to_visible_target"),
            arguments={"target": _text(state.get("target"), 100)})
    elif is_result:
        facts.update(fact_kind="result", public_result=result,
            text=_text(result.get("message"), 600), reason=_text(result.get("reason") or result.get("error"), 160))
    return facts
