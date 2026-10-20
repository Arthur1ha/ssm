"""HMAC authentication for privileged MQTT control-plane messages.

The reusable internal HTTP service token must never be copied into MQTT.  A
dedicated signing secret can be configured, otherwise a domain-separated key
is derived from the internal token.  Signatures are bound to the MQTT topic,
payload, timestamp, and nonce so they cannot be moved to another topic or
replayed within the accepted clock window.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any


AUTH_FIELD = "_mqtt_auth"
ALGORITHM = "HMAC-SHA256"
DEFAULT_MAX_AGE_S = 90
DEFAULT_FUTURE_SKEW_S = 15
_KEY_DERIVATION_LABEL = b"ssm/mqtt-control-plane/v1"


class MqttSignatureError(ValueError):
    """A privileged MQTT message failed authentication."""


def _signing_key(
    *,
    signing_secret: str | None = None,
    internal_token: str | None = None,
) -> bytes:
    dedicated = (
        os.getenv("SSM_MQTT_SIGNING_SECRET", "")
        if signing_secret is None
        else signing_secret
    )
    if dedicated:
        if len(dedicated) < 16:
            raise MqttSignatureError("MQTT signing secret is not configured securely")
        return hmac.new(
            dedicated.encode("utf-8"), _KEY_DERIVATION_LABEL, hashlib.sha256
        ).digest()

    fallback = (
        os.getenv("SSM_INTERNAL_SERVICE_TOKEN", "")
        if internal_token is None
        else internal_token
    )
    if len(fallback) < 16:
        raise MqttSignatureError("MQTT signing secret is not configured")
    return hmac.new(
        fallback.encode("utf-8"), _KEY_DERIVATION_LABEL, hashlib.sha256
    ).digest()


def _canonical_body(payload: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != AUTH_FIELD}
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MqttSignatureError("MQTT payload is not canonical JSON") from exc


def _signature_input(topic: str, timestamp: int, nonce: str, body: bytes) -> bytes:
    return (
        topic.encode("utf-8")
        + b"\n"
        + str(timestamp).encode("ascii")
        + b"\n"
        + nonce.encode("ascii")
        + b"\n"
        + body
    )


def sign_message(
    topic: str,
    payload: Mapping[str, Any],
    *,
    signing_secret: str | None = None,
    internal_token: str | None = None,
    now: float | None = None,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``payload`` carrying a topic-bound HMAC envelope."""
    if not topic or not isinstance(topic, str):
        raise MqttSignatureError("invalid MQTT topic")
    signed = dict(payload)
    signed.pop(AUTH_FIELD, None)
    timestamp = int(time.time() if now is None else now)
    message_nonce = nonce or secrets.token_hex(16)
    if not _valid_nonce(message_nonce):
        raise MqttSignatureError("invalid MQTT nonce")
    key = _signing_key(
        signing_secret=signing_secret, internal_token=internal_token
    )
    signature = hmac.new(
        key,
        _signature_input(topic, timestamp, message_nonce, _canonical_body(signed)),
        hashlib.sha256,
    ).hexdigest()
    signed[AUTH_FIELD] = {
        "alg": ALGORITHM,
        "timestamp": timestamp,
        "nonce": message_nonce,
        "signature": signature,
    }
    return signed


def _valid_nonce(nonce: Any) -> bool:
    return (
        isinstance(nonce, str)
        and 16 <= len(nonce) <= 128
        and nonce.isascii()
        and all(char.isalnum() or char in "-_" for char in nonce)
    )


class NonceReplayGuard:
    """Small process-local replay cache for authenticated MQTT messages."""

    def __init__(self, retention_s: int = DEFAULT_MAX_AGE_S + DEFAULT_FUTURE_SKEW_S):
        self._retention_s = retention_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str, *, now: float) -> bool:
        with self._lock:
            cutoff = now - self._retention_s
            for stale_nonce, seen_at in tuple(self._seen.items()):
                if seen_at < cutoff:
                    self._seen.pop(stale_nonce, None)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


def verify_message(
    topic: str,
    payload: Any,
    *,
    signing_secret: str | None = None,
    internal_token: str | None = None,
    now: float | None = None,
    max_age_s: int = DEFAULT_MAX_AGE_S,
    future_skew_s: int = DEFAULT_FUTURE_SKEW_S,
    replay_guard: NonceReplayGuard | None = None,
) -> dict[str, Any]:
    """Verify an envelope and return the unsigned payload copy."""
    if not isinstance(payload, dict):
        raise MqttSignatureError("MQTT payload must be an object")
    auth = payload.get(AUTH_FIELD)
    if not isinstance(auth, dict) or auth.get("alg") != ALGORITHM:
        raise MqttSignatureError("missing MQTT signature")
    timestamp = auth.get("timestamp")
    nonce = auth.get("nonce")
    supplied = auth.get("signature")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise MqttSignatureError("invalid MQTT signature timestamp")
    if not _valid_nonce(nonce):
        raise MqttSignatureError("invalid MQTT nonce")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise MqttSignatureError("invalid MQTT signature")

    current_time = time.time() if now is None else now
    if timestamp < current_time - max_age_s:
        raise MqttSignatureError("stale MQTT signature")
    if timestamp > current_time + future_skew_s:
        raise MqttSignatureError("future MQTT signature")

    unsigned = {key: value for key, value in payload.items() if key != AUTH_FIELD}
    key = _signing_key(
        signing_secret=signing_secret, internal_token=internal_token
    )
    expected = hmac.new(
        key,
        _signature_input(topic, timestamp, nonce, _canonical_body(unsigned)),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise MqttSignatureError("invalid MQTT signature")
    if replay_guard is not None and not replay_guard.consume(nonce, now=current_time):
        raise MqttSignatureError("replayed MQTT message")
    return unsigned
