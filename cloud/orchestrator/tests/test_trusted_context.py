import pytest

from cloud.mqtt_signing import NonceReplayGuard, sign_message
from trusted_context import (
    TrustedContextError,
    parse_trusted_intent,
    parse_trusted_session_end,
)


SERVICE_TOKEN = "service-secret-123456"


def _payload(*, topic="ssm/intent/ses_a", signing_token=SERVICE_TOKEN, **changes):
    payload = {
        "text": "开灯",
        "context": {
            "session_id": "ses_a",
            "actor": "visitor",
            "persist": False,
            "space_id": "main",
            "issued_at": 900,
            "expires_at": 1100,
        },
    }
    payload.update(changes)
    return sign_message(topic, payload, internal_token=signing_token, now=1000)


def test_accepts_server_context_and_ignores_spoofed_top_level_fields():
    payload = _payload(actor="admin", persist=True, space_id="other")
    result = parse_trusted_intent(payload, "ses_a", token=SERVICE_TOKEN, now=1000)
    assert result["actor"] == "visitor"
    assert result["persist"] is False
    assert result["space_id"] == "main"
    assert result["user_msg"] == "开灯"


def test_rejects_missing_signature_and_tampered_payload():
    with pytest.raises(TrustedContextError):
        parse_trusted_intent({"text": "开灯"}, "ses_a", token=SERVICE_TOKEN, now=1000)

    tampered = _payload()
    tampered["text"] = "关灯"
    with pytest.raises(TrustedContextError, match="signature"):
        parse_trusted_intent(tampered, "ses_a", token=SERVICE_TOKEN, now=1000)

    wrong_key = _payload(signing_token="other-service-secret-1234")
    with pytest.raises(TrustedContextError, match="signature"):
        parse_trusted_intent(wrong_key, "ses_a", token=SERVICE_TOKEN, now=1000)


def test_rejects_when_service_token_is_not_configured():
    with pytest.raises(TrustedContextError, match="not configured"):
        parse_trusted_intent(_payload(), "ses_a", token="", now=1000)


def test_rejects_topic_session_mismatch_visitor_persistence_and_expiry():
    with pytest.raises(TrustedContextError, match="mismatch"):
        parse_trusted_intent(
            _payload(topic="ssm/intent/ses_b"),
            "ses_b",
            token=SERVICE_TOKEN,
            now=1000,
        )

    context = _payload()["context"]
    visitor_write = _payload(context={**context, "persist": True})
    with pytest.raises(TrustedContextError, match="cannot persist"):
        parse_trusted_intent(visitor_write, "ses_a", token=SERVICE_TOKEN, now=1000)

    expired = _payload()
    expired = _payload(context={**expired["context"], "expires_at": 999})
    with pytest.raises(TrustedContextError, match="expired"):
        parse_trusted_intent(expired, "ses_a", token=SERVICE_TOKEN, now=1000)


def test_rejects_stale_future_and_replayed_signatures():
    stale = sign_message(
        "ssm/intent/ses_a",
        {"text": "开灯", "context": _payload()["context"]},
        internal_token=SERVICE_TOKEN,
        now=800,
    )
    with pytest.raises(TrustedContextError, match="stale"):
        parse_trusted_intent(stale, "ses_a", token=SERVICE_TOKEN, now=1000)

    future = sign_message(
        "ssm/intent/ses_a",
        {"text": "开灯", "context": _payload()["context"]},
        internal_token=SERVICE_TOKEN,
        now=1020,
    )
    with pytest.raises(TrustedContextError, match="future"):
        parse_trusted_intent(future, "ses_a", token=SERVICE_TOKEN, now=1000)

    guard = NonceReplayGuard()
    message = _payload()
    parse_trusted_intent(
        message, "ses_a", token=SERVICE_TOKEN, now=1000, replay_guard=guard
    )
    with pytest.raises(TrustedContextError, match="replayed"):
        parse_trusted_intent(
            message, "ses_a", token=SERVICE_TOKEN, now=1000, replay_guard=guard
        )


def test_session_end_signature_is_topic_bound_and_context_bound():
    context = _payload()["context"]
    message = sign_message(
        "ssm/session/ses_a/ended",
        {"context": context, "reason": "logout"},
        internal_token=SERVICE_TOKEN,
        now=1000,
    )
    result = parse_trusted_session_end(
        message, "ses_a", token=SERVICE_TOKEN, now=1000
    )
    assert result == {"session_id": "ses_a", "reason": "logout"}

    with pytest.raises(TrustedContextError, match="signature"):
        parse_trusted_session_end(
            message, "ses_b", token=SERVICE_TOKEN, now=1000
        )


def test_dedicated_signing_secret_does_not_require_internal_token():
    secret = "dedicated-mqtt-secret-1234"
    message = sign_message(
        "ssm/intent/ses_a",
        {"text": "开灯", "context": _payload()["context"]},
        signing_secret=secret,
        internal_token="",
        now=1000,
    )
    result = parse_trusted_intent(
        message,
        "ses_a",
        token="",
        signing_secret=secret,
        now=1000,
    )
    assert result["user_msg"] == "开灯"
