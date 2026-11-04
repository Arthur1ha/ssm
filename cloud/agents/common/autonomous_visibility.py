"""Decide when an autonomous device result deserves an unsolicited reply."""

from __future__ import annotations

import threading
import time
from typing import Hashable


# These outcomes need a person to check the physical situation. Routine
# preflight failures, transient sensor readings and duplicate commands do not.
ACTIONABLE_ERRORS = frozenset({
    "edge_result_unconfirmed", "stop_not_confirmed", "stop_unconfirmed",
    "body_clearance_unavailable", "turn_clearance_unavailable",
})
MATERIAL_REPLY_INTERVAL_S = 120


def actionable_error(result: dict) -> str:
    return next((code for code in (result.get("error"), result.get("reason"))
                 if isinstance(code, str) and code in ACTIONABLE_ERRORS), "")


class AutonomousReplyGate:
    """One device's reply budget for its current activity or autonomous mode."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scope = ""
        self._completed = False
        self._last_material_key: Hashable | None = None
        self._last_reply_at = 0.0
        self._reported_errors: set[str] = set()

    def reset(self) -> None:
        with self._lock:
            self._scope = ""
            self._completed = False
            self._last_material_key = None
            self._last_reply_at = 0.0
            self._reported_errors.clear()

    def _select_scope(self, scope: str) -> None:
        if scope != self._scope:
            self._scope = scope
            self._completed = False
            self._last_material_key = None
            self._last_reply_at = 0.0
            self._reported_errors.clear()

    def _eligible(self, *, completed: bool, material_key: Hashable | None,
                  error: str, now: float) -> bool:
        if error in ACTIONABLE_ERRORS and error not in self._reported_errors:
            return True
        if not completed:
            return False
        if not self._completed:
            return True
        return (material_key is not None
                and material_key != self._last_material_key
                and now - self._last_reply_at >= MATERIAL_REPLY_INTERVAL_S)

    def should_reply(self, scope: str, *, completed: bool = False,
                     material_key: Hashable | None = None, error: str = "",
                     now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        with self._lock:
            self._select_scope(scope)
            return self._eligible(completed=completed, material_key=material_key,
                                  error=error, now=instant)

    def mark_replied(self, scope: str, *, completed: bool = False,
                     material_key: Hashable | None = None, error: str = "",
                     now: float | None = None) -> bool:
        """Recheck after wording is ready, then atomically claim the reply."""
        instant = time.time() if now is None else now
        with self._lock:
            self._select_scope(scope)
            if not self._eligible(completed=completed, material_key=material_key,
                                  error=error, now=instant):
                return False
            if completed:
                self._completed = True
                self._last_material_key = material_key
            if error:
                self._reported_errors.add(error)
            self._last_reply_at = instant
            return True
