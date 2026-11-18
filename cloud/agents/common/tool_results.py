"""Task outcomes and termination shared by every role's Graph.

Device adapters keep their transport receipts. The Graph projects an accepted
command as task success and owns whether that success ends this run.
"""

SUCCESS = {"ok", "accepted"}
UNCERTAIN = {"executing", "timeout", "unknown"}
FAILURE = {"blocked", "rejected", "cancelled", "error", "partial", "needs_clarification"}


def prior_tool_step(steps, name: str) -> dict | None:
    """Find an effect that completed or still needs reconciliation, not a failed attempt."""
    return next((step for step in reversed(list(steps))
                 if step.get("tool") == name
                 and ((step.get("result") or {}).get("result") in SUCCESS | UNCERTAIN
                      or step.get("dispatch_status") in {"pending", "unknown"})), None)


def model_error_details(exc: Exception) -> dict:
    """Keep provider diagnostics, without copying request bodies or response IDs."""
    from openai import APIError

    if not isinstance(exc, APIError):
        return {}
    details = {"error_type": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if type(status) is int:
        details["status_code"] = status
    body = exc.body
    error = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(error, dict):
        for source, target in (("message", "message"), ("code", "error_code")):
            value = error.get(source)
            if isinstance(value, str) and value.strip():
                details[target] = value.strip()
    # Connection/timeout errors have no response body. Their SDK message still
    # describes the failure, without serializing the HTTP request.
    if "message" not in details:
        details["message"] = exc.message
    return details


def task_result(receipt: object, *, final: bool) -> dict:
    """Validate an adapter result and make its run-local ending explicit."""
    if (not isinstance(receipt, dict)
            or not isinstance(receipt.get("result"), str)
            or receipt.get("result") not in SUCCESS | UNCERTAIN | FAILURE
            or ("final" in receipt and type(receipt["final"]) is not bool)):
        return {"result": "error", "error": "invalid_tool_result", "final": True}
    result = dict(receipt)
    if result["result"] == "accepted":
        result.update(result="ok", device_result="accepted")
        result.pop("error", None)
    # A domain executor may explicitly end even a failed task. Otherwise only
    # a successful action can satisfy the caller's planned final step.
    result.setdefault("final", result["result"] == "cancelled"
                      or (final and result["result"] == "ok"))
    return result
