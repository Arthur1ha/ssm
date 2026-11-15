"""Task outcomes and termination shared by every role's Graph.

Device adapters keep their transport receipts. The Graph projects an accepted
command as task success and owns whether that success ends this run.
"""

SUCCESS = {"ok", "accepted"}
UNCERTAIN = {"executing", "timeout", "unknown"}
FAILURE = {"blocked", "rejected", "cancelled", "error", "partial", "needs_clarification"}


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
