"""Small, factual step details for the browser's progress display."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field


class PublicDecisionExplanation(BaseModel):
    """Optional public metadata from the same decision, never observation facts."""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision_reason: str = Field(min_length=1, max_length=400)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)


def resolve_public_decision(space: dict, reason: object, refs: object, *,
                            observed_space: dict | None = None) -> dict:
    """Validate a short explanation; resolve every supplied reference or reject it.

    A reason without references is just the model's public statement. It cannot
    acquire fabricated evidence, and does not satisfy mandatory game evidence.
    """
    explanation = PublicDecisionExplanation(
        decision_reason=reason.strip() if isinstance(reason, str) else reason,
        evidence_refs=[] if refs is None else refs)
    if explanation.evidence_refs:
        from cloud.space.decisions import resolve_decision
        return resolve_decision(space, explanation.decision_reason,
                                explanation.evidence_refs, observed_space=observed_space)
    return {"reason": explanation.decision_reason, "evidence": []}


# This is a public projection, never a generic serializer of runtime state.
_ID_FIELDS = ("event_id", "source_event_id", "trace_id", "run_id", "parent_run_id",
              "step_id", "tool_call_id", "task_id", "request_id", "agent_id",
              "target_unit_id", "trigger_message_id", "reply_to", "conversation_id",
              "observation_event_id", "observation_id", "transition_id", "device_epoch",
              "frame_id", "teaching_id", "training_ref", "activity_id", "question_id", "game_instance_id")
_RESULT_FIELDS = {
    "action": {"ism_state", "confirmation", "device_connected", "execution_phase",
               "completion_basis", "dispatch_only", "skipped", "goal_state",
               "progress", "motion_performed", "cargo_motion_confirmed", "delivered",
               "directional_push_attempted", "push_direction_precision", "push_heading_rad"},
    "query": {"source", "ism_state", "connected", "connection_state", "life_state", "body_state",
              "battery_soc", "found", "count", "as_of", "frame_id"},
    "message": {"message_id", "request_id", "delivery_status", "conversation_id", "reply_to"},
    "activity": {"activity_id", "revision", "status", "stop_confirmed"},
    "configuration": {"constraint_id", "expires_at", "saved", "loaded"},
    "training": {"teaching_id", "training_ref", "revision", "training_revision", "name",
                 "step_index", "step_number", "step_count", "saved", "loaded", "skipped"},
    "training_proposal": {"teaching_id", "training_ref", "revision", "name", "saved", "loaded"},
    "search": {"found", "frame_id", "as_of", "candidate", "stop_confirmed", "verified"},
}


def _scalar(value, limit=400):
    if isinstance(value, str):
        return value[:limit]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def public_decision_basis(value: object) -> dict:
    """Bound an already server-resolved explanation; never resolve model evidence here."""
    if not isinstance(value, dict) or not isinstance(value.get("reason"), str):
        return {}
    reason = value["reason"].strip()[:400]
    if not reason:
        return {}
    evidence = []
    entries = value.get("evidence")
    for item in (entries[:8] if isinstance(entries, list) else []):
        if not isinstance(item, dict):
            continue
        clean = {key: _scalar(item[key], 240) for key in
                 ("ref", "kind", "source", "as_of", "summary", "status", "validity") if key in item}
        if clean.get("ref") and clean.get("summary"):
            evidence.append(clean)
    return {"reason": reason, "evidence": evidence}


def public_tool_result(value: object, *, result_kind="action", depth=0) -> dict:
    """Typed, bounded receipt facts; no arbitrary result/context/memory dictionaries."""
    if not isinstance(value, dict) or depth > 2:
        return {}
    fields = {"result", "status", "error", "reason", "message", "error_type", "error_code", "status_code", "task_id", "request_id",
              "final", "dispatch_only", "unit_id", "result_kind"}
    fields |= _RESULT_FIELDS.get(result_kind, _RESULT_FIELDS["action"])
    failed = value.get("result") in {"error", "partial", "blocked", "rejected", "timeout", "unknown"}
    result = {key: _scalar(value[key], 8192 if key == "message" and failed else 400) for key in fields if key in value
              and isinstance(value[key], (str, bool, int, float))}
    # Activity success is only the outer lifecycle result. Member failures stay visible.
    for key in ("members", "member_results", "join_results", "stop_results"):
        members = value.get(key)
        if result_kind != "activity" or not isinstance(members, (list, dict)):
            continue
        entries = list(members.items())[:16] if isinstance(members, dict) else [(None, x) for x in members[:16]]
        projected = []
        for unit_id, item in entries:
            if not isinstance(item, dict):
                continue
            clean = {field: _scalar(item[field]) for field in
                     ("unit_id", "agent_id", "ok", "result", "status", "reason", "error", "mode", "stop_confirmed")
                     if field in item and isinstance(item[field], (str, bool, int, float))}
            if unit_id is not None:
                clean.setdefault("unit_id", str(unit_id)[:120])
            if clean:
                projected.append(clean)
        if projected:
            result[key] = projected
    if result_kind == "activity" and depth < 2:
        activity = value.get("activity")
        if isinstance(activity, dict):
            result["activity"] = public_tool_result({**activity,
                "members": activity.get("devices") or activity.get("members")},
                result_kind="activity", depth=depth + 1)
    if result_kind in {"training", "action"} and isinstance(value.get("tool_results") or value.get("steps"), list):
        receipts = value.get("tool_results") or value.get("steps")
        result["steps"] = [{**{key: _scalar(item[key], 120) for key in
                           ("step_id", "request_id", "tool", "action", "step_index") if key in item},
                           "result": public_tool_result(item.get("result"), depth=depth + 1)}
                          for item in receipts[:16] if isinstance(item, dict)]
    observations = value.get("observations")
    if result_kind == "query" and isinstance(observations, list):
        from cloud.space.context import _VALUES
        result["observations"] = []
        for item in observations[:24]:
            if not isinstance(item, dict):
                continue
            projected = {key: _scalar(item[key], 120) for key in ("source", "status", "as_of") if key in item}
            values = item.get("values")
            if isinstance(values, dict):
                projected["values"] = {key: _scalar(values[key], 120) for key in _VALUES
                    if key in values and (values[key] is None or isinstance(values[key], (str, bool, int, float)))}
            result["observations"].append(projected)
    agents = value.get("agents")
    if result_kind == "query" and isinstance(agents, list):
        result["agents"] = [{key: _scalar(item[key], 120) for key in
            ("unit_id", "name", "online", "agent_type") if key in item}
            for item in agents[:24] if isinstance(item, dict)]
    candidate = value.get("candidate")
    if result_kind in {"search", "query", "action"} and isinstance(candidate, dict):
        result["candidate"] = {key: _scalar(candidate[key], 160) for key in
            ("source", "view_kind", "x", "y", "heading", "name", "label", "frame_id", "as_of")
            if key in candidate and isinstance(candidate[key], (str, int, float, bool))}
    rules = value.get("game_rules")
    if isinstance(rules, dict):
        # Only this game's actual agreements belong in progress, not the
        # static instructions used by the role models and executors.
        result["game_rules"] = {key: _scalar(rules[key], 160) for key in
            ("rule_id", "status", "game_id", "round", "name") if key in rules}
        if isinstance(rules.get("mapping"), list):
            result["game_rules"]["mapping"] = [{key: _scalar(item[key], 120) for key in
                ("choice", "label") if key in item} for item in rules["mapping"][:16]
                if isinstance(item, dict)]
    return result


def step_progress_facts(step: dict) -> dict:
    from cloud.agents.common.replies import step_kind

    params = step.get("params") or {}
    result = step.get("result") or {}
    target = params.get("unit_id") or params.get("target_unit_id") or result.get("unit_id") or ""
    skill = params.get("skill_id") or ""
    arguments = params.get("params") if skill else params
    kind = step_kind(step)
    value = {**{key: step[key] for key in (*_ID_FIELDS, "sequence", "model_turn", "ts") if key in step},
             "tool": step.get("tool") or "", "target_unit_id": target,
             "skill_id": skill, "arguments": arguments, "result_kind": kind,
             "fact_kind": "result" if result else "call",
             "public_result": public_tool_result(result, result_kind=kind),
             "decision_basis": step.get("decision_basis") or result.get("decision_basis"),
             "reason": next((result[key] for key in ("message", "error", "reason")
                            if isinstance(result.get(key), str) and result[key]), ""),
             "result_state": result.get("ism_state") or ""}
    if result.get("dispatch_only") is True:
        value["dispatch_only"] = True
    return public_process_facts(value)


def canonical_capability(value: str) -> str:
    """Normalize display identifiers, never commands sent to the device."""
    from cloud.esp32.tool_registry import LAMP_SKILLS

    for action, skill in LAMP_SKILLS.items():
        if value in (action, skill["tool"], skill["id"]):
            return skill["tool"]
    return value


def public_arguments(value: object, *, tool: str = "", target: str = "", skill_id: str = "") -> dict:
    """Bound public scalar arguments; no prompts, credentials or context blobs."""
    if not isinstance(value, dict):
        return {}
    from cloud.esp32.tool_registry import TOOL_DECLARATIONS
    ability = canonical_capability(skill_id or tool)
    schema = (TOOL_DECLARATIONS.get(ability) or {}).get("params_schema")
    if schema is None and ability.startswith("go2_"):
        from cloud.go2.tools.registry import get_tool_spec
        spec = get_tool_spec(ability)
        schema = spec.params_schema if spec else None
    if schema is None and target and skill_id:
        from cloud.cards.registry import get_registry
        card = get_registry().get_card(target) or {}
        skill = next((item for item in card.get("skills", []) if item.get("id") == skill_id), {})
        schema = skill.get("params_schema")
    if schema is None:
        from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS
        from cloud.agents.common.training.tools import SCHEMAS as training_schemas
        declaration = {**STEWARD_TOOL_SCHEMAS, **training_schemas}.get(ability)
        if declaration is not None:
            schema = declaration.model_json_schema()
    fields = (schema or {}).get("properties", {})
    private = {"content", "message", "rule", "actor", "persist", "requester", "context",
               "token", "secret", "password", "authorization", "prompt", "api_key",
               "decision_reason", "evidence_refs"}
    result = {}
    for key, item in list(value.items())[:32]:
        if (not isinstance(key, str) or key not in fields or len(key) > 64 or key.lower() in private
                or any(part in key.lower() for part in ("token", "secret", "password", "credential"))):
            continue
        if isinstance(item, str) and len(item) <= 240 or type(item) in (bool, int) or type(item) is float and math.isfinite(item):
            result[key] = item
        elif isinstance(item, list) and len(item) <= 16 and all(
                isinstance(entry, str) and len(entry) <= 120 or type(entry) in (bool, int)
                or type(entry) is float and math.isfinite(entry) for entry in item):
            result[key] = item
    return result


def public_process_facts(value: dict) -> dict:
    """One projection for live agent events and persisted activity details."""
    from cloud.esp32.tool_registry import LAMP_SKILLS

    result = {}
    for key in (*_ID_FIELDS, "skill_id", "tool", "action", "requested_text", "result_state",
                "fact_kind", "result_kind", "status", "capability_name", "reason", "name",
                "observation_summary", "media_ref", "stage", "reply_kind", "reply_source", "question", "context_scope"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = canonical_capability(item[:120]) if key in {"action", "tool"} else item[:400]
    for key in ("sequence", "model_turn", "ts", "as_of", "version", "revision",
                "training_revision", "step_number", "step_index", "completed_count", "failed_count", "goal_revision", "frame_id"):
        item = value.get(key)
        if type(item) in (int, float) and math.isfinite(item):
            result[key] = item
    observation = value.get("observation")
    if isinstance(observation, dict):
        fields = ("found", "frame_id", "as_of", "scene_summary", "proximity_reason", "proximity")
        if result.get("result_kind") == "training":
            fields += ("event", "detected", "recognized", "target")
        result["observation"] = {key: _scalar(observation[key], 240) for key in fields
            if key in observation and (observation[key] is None or
                isinstance(observation[key], (str, bool, int, float)))}
    if type(value.get("dispatch_only")) is bool:
        result["dispatch_only"] = value["dispatch_only"]
    basis = public_decision_basis(value.get("decision_basis"))
    if basis:
        result["decision_basis"] = basis
    kind = result.get("result_kind", "action")
    receipt = public_tool_result(value.get("public_result"), result_kind=kind)
    if receipt:
        result["public_result"] = receipt
    if isinstance(value.get("arguments"), dict):
        result["arguments"] = public_arguments(value["arguments"],
            tool=result.get("tool") or result.get("action") or "",
            target=result.get("target_unit_id") or "", skill_id=result.get("skill_id") or "")
    ability = result.get("skill_id") or result.get("tool") or result.get("action")
    canonical = canonical_capability(ability or "")
    for skill in LAMP_SKILLS.values():
        if canonical == skill["tool"]:
            result["capability_name"] = skill["name"]
            break
    if ability and canonical.startswith("go2_"):
        from cloud.go2.tools.registry import tool_display_name
        label = tool_display_name(canonical)
        if label:
            result["capability_name"] = label
    if ability and "capability_name" not in result:
        from cloud.cards.registry import get_registry
        unit_id = result.get("target_unit_id") or value.get("unit_id") or value.get("agent_id")
        card = get_registry().get_card(unit_id) if unit_id else None
        for skill in (card or {}).get("skills", []):
            if skill.get("id") == ability:
                result["capability_name"] = str(skill.get("name") or ability)[:120]
                break
    return result


def training_step_facts(record: dict, resolved: dict, *, run_id: str,
                        phase: str, result: dict | None = None,
                        observation: dict | None = None) -> dict:
    """Link a saved behavior's observation, dispatch and receipt without inferring posture.

    The observation is attached by the trusted listener, never by a model or
    browser. Its original timestamp remains intact through later steps.
    """
    import uuid

    index = resolved["step_index"]
    sequence = index * 3 + {"observation": 0, "call": 1, "result": 2}[phase]
    identity = f'{run_id}:{record["teaching_id"]}:{record["revision"]}:{index}:{phase}'
    facts = {
        "source_event_id": "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
        "run_id": run_id, "step_id": f"{run_id}:{index}", "sequence": sequence,
        "teaching_id": record["teaching_id"], "version": record["revision"],
        "revision": record["revision"], "training_revision": record["revision"],
        "transition_id": resolved.get("transition_id"),
        "step_index": index, "step_number": index + 1,
        "name": record.get("name") or "", "result_kind": "training",
        "fact_kind": phase, "context_scope": "process",
        "stage": "executing" if phase == "call" else "observed",
        "status": (result or {}).get("result", "unknown") if phase == "result" else
                  "executing" if phase == "call" else "observed",
        "public_result": result or {},
    }
    trigger = (record.get("definition") or {}).get("trigger") or {}
    observed = observation if isinstance(observation, dict) else {}
    ts = observed.get("ts")
    if (trigger.get("event_id") and observed.get("id") == trigger["event_id"]
            and isinstance(observed.get("event_id"), str) and observed["event_id"]
            and type(ts) in (int, float) and math.isfinite(ts) and ts >= 0):
        facts.update(observation_event_id=observed["event_id"],
                     observation_id=observed["id"], as_of=ts)
        frame_id = observed.get("frame_id")
        if isinstance(frame_id, str) or type(frame_id) is int:
            facts["frame_id"] = frame_id
        values = observed.get("values") if isinstance(observed.get("values"), dict) else {}
        facts["observation"] = {"event": observed["id"], "as_of": ts,
                                **{key: values[key] for key in ("detected", "recognized", "target")
                                   if key in values}}
    return public_process_facts(facts)


def progress_event_facts(run, phase: str, step: dict | None) -> dict:
    """Stable source identity for one original node update, including replays."""
    import uuid

    item = step or {}
    sequence = item.get("sequence", run.model_turn * 1000 + len(run.steps) * 10)
    sequence += {"decision": 0, "planned": 0, "dispatching": 1,
                 "executing": 1, "observed": 2, "considering": 4,
                 "summarizing": 8, "finished": 9}.get(phase, 0)
    identity = f"{run.run_id}:{run.attempt}:{item.get('step_id', '')}:{phase}:{sequence}"
    event_id = "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex
    fields = step_progress_facts(item) if step else {}
    fields.update(event_id=event_id, source_event_id=event_id,
                  goal_revision=getattr(run, "goal_revision", 0),
                  run_id=run.run_id, trace_id=run.trace_id or run.run_id,
                  parent_run_id=run.parent_run_id, agent_id=run.agent_id,
                  model_turn=item.get("model_turn", run.model_turn), sequence=sequence,
                  stage=phase, status=(item.get("result") or {}).get("result") or
                  (run.status if phase == "finished" else phase))
    if step:
        fields["step_number"] = next((i + 1 for i, value in enumerate(run.steps)
                                     if value.get("step_id") == item.get("step_id")), len(run.steps))
    return fields
