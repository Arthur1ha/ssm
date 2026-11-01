"""Trusted steward intent context."""

def build_intent_run_context(trusted: dict) -> dict:
    """Carry every authenticated intent field into the worker, including trace_id."""
    return {**trusted, "requirements": [], "route": trusted.get("intent_hint", ""),
            "planned_tasks": [], "rule": {}, "task_results": {},
            "response_text": "", "early_exit": False}
