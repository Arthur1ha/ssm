"""Signed single-hop coordination enters the steward conversation Graph."""

import time

from cloud.agents.steward.graph import run_intent_loop
from cloud.orchestrator import tools as transport

def run_peer_loop(trusted: dict) -> dict:
    """Carry signed peer provenance into each revalidated child skill call."""
    request_id = trusted["request_id"]
    session_id = f"peer_{request_id}"
    now = time.time()
    base = {"request_id": request_id,
            "initiator_unit_id": trusted["initiator_unit_id"],
            "target_unit_id": trusted["target_unit_id"], "ts": int(now)}
    if trusted["expires_at"] <= now:
        return {**base, "result": "timeout", "response": "请求已过期。", "task_results": {}}
    context = {"session_id": session_id,
               "actor": "agent" if trusted["origin_kind"] == "agent_autonomous" else trusted["actor"],
               "persist": False, "space_id": trusted["space_id"],
               "origin_kind": trusted["origin_kind"],
               "origin_session_id": trusted.get("origin_session_id"),
               "peer_request_id": request_id,
               "trace_id": trusted.get("trace_id"),
               "parent_run_id": trusted.get("parent_run_id"),
               "expires_at": trusted["expires_at"],
               "user_msg": trusted["params"]["message"], "event_id": request_id}
    if transport._state:
        transport._state.register_session(context)
    try:
        with transport.peer_feedback_scope():
            completed = run_intent_loop(context)
        steps = completed.get("steps", [])
        results = {step["step_id"]: step.get("result") or {"result": "unknown"}
                   for step in steps if step.get("tool") == "invoke_skill"}
        status = completed["result"]
        if status == "partial":
            status = next((r.get("result") for r in results.values() if r.get("result") != "ok"), "blocked")
        return {**base, "trace_id": completed.get("trace_id"),
                "run_id": completed.get("run_id"),
                "parent_run_id": trusted.get("parent_run_id"),
                "result": status, "response": completed.get("response", ""),
                "task_results": results, "ts": int(time.time())}
    finally:
        if transport._state:
            transport._state.clear_session(session_id)
