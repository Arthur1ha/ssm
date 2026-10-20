"""Validation for signed messages sent from the FastAPI command gateway."""

import hmac
import time
from typing import Any

from cloud.mqtt_signing import (
    MqttSignatureError,
    NonceReplayGuard,
    verify_message,
)


MAIN_SPACE_ID = "main"
VALID_ACTORS = frozenset(("admin", "visitor"))
MAX_CLOCK_SKEW_S = 60


class TrustedContextError(ValueError):
    """The message did not carry a valid server-issued execution context."""


def parse_trusted_intent(
    payload: Any,
    topic_session_id: str,
    *,
    token: str | None = None,
    signing_secret: str | None = None,
    now: float | None = None,
    replay_guard: NonceReplayGuard | None = None,
) -> dict:
    """Validate and normalize one internal intent message.

    Browser-supplied top-level ``actor``, ``persist`` and ``space_id`` are
    never consulted.  Only the authenticated nested context is returned.
    """
    topic = f"ssm/intent/{topic_session_id}"
    try:
        unsigned = verify_message(
            topic,
            payload,
            internal_token=token,
            signing_secret=signing_secret,
            now=now,
            replay_guard=replay_guard,
        )
    except MqttSignatureError as exc:
        raise TrustedContextError(str(exc)) from exc
    context = unsigned.get("context")
    if not isinstance(context, dict):
        raise TrustedContextError("missing trusted context")

    session_id = context.get("session_id")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise TrustedContextError("invalid session_id")
    if not hmac.compare_digest(session_id, str(topic_session_id)):
        raise TrustedContextError("topic/context session mismatch")

    actor = context.get("actor")
    if actor not in VALID_ACTORS:
        raise TrustedContextError("invalid actor")
    persist = context.get("persist")
    if not isinstance(persist, bool):
        raise TrustedContextError("persist must be boolean")
    if actor == "visitor" and persist:
        raise TrustedContextError("visitor context cannot persist")
    if context.get("space_id") != MAIN_SPACE_ID:
        raise TrustedContextError("invalid space_id")

    current_time = time.time() if now is None else now
    issued_at = context.get("issued_at")
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        raise TrustedContextError("invalid issued_at")
    if issued_at <= 0 or issued_at > current_time + MAX_CLOCK_SKEW_S:
        raise TrustedContextError("invalid issued_at")
    expires_at = context.get("expires_at")
    if expires_at is not None:
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            raise TrustedContextError("invalid expires_at")
        if expires_at <= current_time or expires_at <= issued_at:
            raise TrustedContextError("session expired")

    text = unsigned.get("text")
    if not isinstance(text, str) or not text.strip():
        raise TrustedContextError("missing intent text")

    return {
        "session_id": session_id,
        "actor": actor,
        "persist": persist,
        "space_id": MAIN_SPACE_ID,
        "issued_at": float(issued_at),
        "expires_at": float(expires_at) if expires_at is not None else None,
        "user_msg": text.strip(),
        "requirements": [],
        "intent_hint": unsigned.get("intent_hint", ""),
        "device_hint": unsigned.get("device_hint", ""),
    }


def parse_trusted_session_end(
    payload: Any,
    topic_session_id: str,
    *,
    token: str | None = None,
    signing_secret: str | None = None,
    now: float | None = None,
    replay_guard: NonceReplayGuard | None = None,
) -> dict[str, Any]:
    """Authenticate and normalize a session lifecycle cleanup message."""
    topic = f"ssm/session/{topic_session_id}/ended"
    try:
        unsigned = verify_message(
            topic,
            payload,
            internal_token=token,
            signing_secret=signing_secret,
            now=now,
            replay_guard=replay_guard,
        )
    except MqttSignatureError as exc:
        raise TrustedContextError(str(exc)) from exc

    context = unsigned.get("context")
    if not isinstance(context, dict):
        raise TrustedContextError("missing trusted context")
    session_id = context.get("session_id")
    if not isinstance(session_id, str) or not hmac.compare_digest(
        session_id, str(topic_session_id)
    ):
        raise TrustedContextError("topic/context session mismatch")
    if context.get("actor") not in VALID_ACTORS:
        raise TrustedContextError("invalid actor")
    if context.get("space_id") != MAIN_SPACE_ID:
        raise TrustedContextError("invalid space_id")
    reason = unsigned.get("reason")
    if not isinstance(reason, str) or not reason or len(reason) > 64:
        raise TrustedContextError("invalid cleanup reason")
    return {"session_id": session_id, "reason": reason}
