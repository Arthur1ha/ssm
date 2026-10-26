"""Validation for signed messages sent from the FastAPI command gateway."""

import hmac
import re
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
PEER_COORDINATOR_ID = "cloud_orchestrator"
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


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
    resume_run_id = unsigned.get("resume_run_id")
    if resume_run_id is not None and (not isinstance(resume_run_id, str)
                                      or _REQUEST_ID.fullmatch(resume_run_id) is None):
        raise TrustedContextError("invalid resume_run_id")

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
        "event_id": unsigned.get("event_id", ""),
        "resume_run_id": resume_run_id,
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


def parse_trusted_peer_request(
    payload: Any,
    topic_request_id: str,
    *,
    token: str | None = None,
    signing_secret: str | None = None,
    now: float | None = None,
    replay_guard: NonceReplayGuard | None = None,
) -> dict[str, Any]:
    """Authenticate an API-issued, single-hop request to the coordinator."""
    if not isinstance(topic_request_id, str) or not _REQUEST_ID.fullmatch(topic_request_id):
        raise TrustedContextError("invalid request_id")
    topic = f"ssm/peer/request/{topic_request_id}"
    try:
        unsigned = verify_message(
            topic, payload, internal_token=token, signing_secret=signing_secret,
            now=now, replay_guard=replay_guard,
        )
    except MqttSignatureError as exc:
        raise TrustedContextError(str(exc)) from exc

    if unsigned.get("request_id") != topic_request_id:
        raise TrustedContextError("topic/request mismatch")
    initiator = unsigned.get("initiator_unit_id")
    if not isinstance(initiator, str) or not _REQUEST_ID.fullmatch(initiator):
        raise TrustedContextError("invalid initiator_unit_id")
    if unsigned.get("target_unit_id") != PEER_COORDINATOR_ID:
        raise TrustedContextError("invalid target_unit_id")
    if unsigned.get("skill_id") != "coordinate_request":
        raise TrustedContextError("invalid skill_id")
    if unsigned.get("space_id") != MAIN_SPACE_ID:
        raise TrustedContextError("invalid space_id")
    if unsigned.get("persist") is not False:
        raise TrustedContextError("peer coordination cannot persist")

    origin_kind = unsigned.get("origin_kind")
    actor = unsigned.get("actor")
    origin_session_id = unsigned.get("origin_session_id")
    if origin_kind == "user_delegated":
        if actor not in VALID_ACTORS or not isinstance(origin_session_id, str) or not origin_session_id or len(origin_session_id) > 128:
            raise TrustedContextError("invalid delegated context")
    elif origin_kind == "agent_autonomous":
        if actor != "agent" or origin_session_id is not None:
            raise TrustedContextError("invalid autonomous context")
    else:
        raise TrustedContextError("invalid origin_kind")

    params = unsigned.get("params")
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, str) or not message.strip() or len(message) > 2048:
        raise TrustedContextError("invalid coordination message")
    current_time = time.time() if now is None else now
    issued_at = unsigned.get("issued_at")
    expires_at = unsigned.get("expires_at")
    if (not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool)
            or issued_at <= 0 or issued_at > current_time + MAX_CLOCK_SKEW_S):
        raise TrustedContextError("invalid issued_at")
    if (not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool)
            or expires_at <= current_time or expires_at <= issued_at
            or expires_at > current_time + 120):
        raise TrustedContextError("invalid expires_at")
    return {
        "request_id": topic_request_id,
        "initiator_unit_id": initiator,
        "target_unit_id": PEER_COORDINATOR_ID,
        "skill_id": "coordinate_request",
        "params": {"message": message.strip()},
        "origin_kind": origin_kind,
        "origin_session_id": origin_session_id,
        "actor": actor,
        "persist": False,
        "space_id": MAIN_SPACE_ID,
        "issued_at": float(issued_at),
        "expires_at": float(expires_at),
    }


def parse_trusted_peer_cancel(
    payload: Any, topic_request_id: str, *, token: str | None = None,
    signing_secret: str | None = None, now: float | None = None,
    replay_guard: NonceReplayGuard | None = None,
) -> dict[str, Any]:
    """Authenticate one API-issued cancellation of pending coordination work."""
    if not _REQUEST_ID.fullmatch(str(topic_request_id)):
        raise TrustedContextError("invalid request_id")
    topic = f"ssm/peer/cancel/{topic_request_id}"
    try:
        unsigned = verify_message(topic, payload, internal_token=token,
                                  signing_secret=signing_secret, now=now,
                                  replay_guard=replay_guard)
    except MqttSignatureError as exc:
        raise TrustedContextError(str(exc)) from exc
    if unsigned.get("request_id") != topic_request_id:
        raise TrustedContextError("topic/request mismatch")
    reason = unsigned.get("reason")
    if reason not in {"user_takeover", "session_ended", "mode_changed", "target_offline"}:
        raise TrustedContextError("invalid cancel reason")
    ts = unsigned.get("ts")
    instant = time.time() if now is None else now
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or abs(ts - instant) > MAX_CLOCK_SKEW_S:
        raise TrustedContextError("stale cancel request")
    return {"request_id": topic_request_id, "reason": reason}
