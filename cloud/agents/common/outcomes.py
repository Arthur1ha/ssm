"""Project completed retries without erasing the original tool receipts."""

from __future__ import annotations

from collections.abc import Iterable


_LAMP_ACTIONS = {"set_led_state", "set_led_color", "blink_led"}
_EXPLANATION_FIELDS = {"decision_reason", "evidence_refs"}


def _lamp_request(step: dict) -> tuple | None:
    """Only compare the physical arguments of one lamp's native tools."""
    if step.get("tool") not in _LAMP_ACTIONS:
        return None
    params = step.get("params")
    if not isinstance(params, dict):
        return None
    return (step["tool"], {key: value for key, value in params.items()
                           if key not in _EXPLANATION_FIELDS and value is not None})


def recovered_attempts(steps: list[dict]) -> dict[int, int]:
    """Match consecutive, certainly unexecuted attempts to a true completion.

    A different effect breaks the chain: a later action cannot retroactively
    satisfy an earlier ordering requirement. Unknown dispatch and physical
    failures remain unresolved. The adapter, never the model, marks preflight.
    """
    recovered: dict[int, int] = {}
    pending: list[int] = []
    request = None
    for index, step in enumerate(steps):
        if step.get("effectful") is False:
            continue
        current = _lamp_request(step)
        if current != request:
            pending = []
            request = current
        result = step.get("result") or {}
        if current is None or step.get("dispatch_status") != "observed":
            pending = []
        elif result.get("result") == "ok":
            recovered.update((failed, index) for failed in pending)
            pending = []
        elif (result.get("result") in {"blocked", "rejected"}
              and result.get("execution_phase") == "preflight"):
            pending.append(index)
        else:
            pending = []
    return recovered


def effective_steps(steps: Iterable[dict]) -> list[dict]:
    """Keep successful and unresolved steps; leave the stored list untouched."""
    entries = list(steps)
    recovered = recovered_attempts(entries)
    return [step for index, step in enumerate(entries) if index not in recovered]


def outcome_summary(steps: list[dict]) -> dict:
    recovered = recovered_attempts(steps)
    return {
        "recovered_attempts": [{"step_id": steps[index].get("step_id"),
                                "completed_by": steps[completed].get("step_id")}
                               for index, completed in recovered.items()],
        "unresolved_attempts": [{"step_id": step.get("step_id"),
                                 "result": (step.get("result") or {}).get("result", "unknown")}
                                for index, step in enumerate(steps)
                                if index not in recovered
                                and (step.get("result") or {}).get("result") != "ok"],
    }


def completed_status(steps: Iterable[dict]) -> str:
    """A model final does not make unresolved tool outcomes successful."""
    return ("partial" if any((step.get("result") or {}).get("result") != "ok"
                             for step in effective_steps(steps)) else "ok")
