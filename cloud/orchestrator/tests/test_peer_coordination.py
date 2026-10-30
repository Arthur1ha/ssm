"""Signed peer coordination stays separate from browser intents."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tools
from trusted_context import (TrustedContextError, parse_trusted_peer_cancel,
                             parse_trusted_peer_request)
from cloud.mqtt_signing import NonceReplayGuard, sign_message
from cloud.orchestrator.agentcore.peer.cancellation import (
    cancel_peer_coordination, clear_peer_coordination, peer_coordination_cancelled,
)


TOKEN = "service-secret-123456"


@pytest.fixture(autouse=True)
def configured_space(monkeypatch):
    monkeypatch.setattr("cloud.orchestrator.agentcore.tools.executor.get_space_registry",
                        lambda: MagicMock(get_space=lambda: {"device_ids": ["esp32_desk", "go2"]}))


def _signed_peer(*, origin_kind="user_delegated", **updates):
    body = {
        "request_id": "req_a", "initiator_unit_id": "go2",
        "target_unit_id": "cloud_orchestrator", "skill_id": "coordinate_request",
        "params": {"message": "请先开灯再打招呼"},
        "origin_kind": origin_kind,
        "origin_session_id": "user_a" if origin_kind == "user_delegated" else None,
        "actor": "visitor" if origin_kind == "user_delegated" else "agent",
        "persist": False, "space_id": "main", "issued_at": 1000,
        "expires_at": 1060,
    }
    body.update(updates)
    return sign_message("ssm/peer/request/req_a", body, internal_token=TOKEN, now=1000)


def test_peer_request_requires_signed_bound_context_and_fresh_nonce():
    guard = NonceReplayGuard()
    signed = _signed_peer()
    trusted = parse_trusted_peer_request(signed, "req_a", token=TOKEN,
                                         now=1000, replay_guard=guard)
    assert trusted["actor"] == "visitor"
    assert trusted["persist"] is False
    with pytest.raises(TrustedContextError, match="replayed"):
        parse_trusted_peer_request(signed, "req_a", token=TOKEN,
                                   now=1000, replay_guard=guard)
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_request(signed, "req_b", token=TOKEN, now=1000)
    signed["actor"] = "admin"
    with pytest.raises(TrustedContextError, match="signature"):
        parse_trusted_peer_request(signed, "req_a", token=TOKEN, now=1000)


def test_peer_cancellation_requires_fresh_signed_request():
    topic = "ssm/peer/cancel/req_a"
    payload = {"request_id": "req_a", "reason": "user_takeover", "ts": 1000}
    signed = sign_message(topic, payload, internal_token=TOKEN, now=1000)
    guard = NonceReplayGuard()
    assert parse_trusted_peer_cancel(signed, "req_a", token=TOKEN, now=1000,
                                     replay_guard=guard)["reason"] == "user_takeover"
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_cancel(signed, "req_a", token=TOKEN, now=1000,
                                  replay_guard=guard)
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_cancel(signed, "req_b", token=TOKEN, now=1000)


@pytest.mark.parametrize("updates", [
    {"persist": True},
    {"space_id": "other"},
    {"origin_session_id": None},
    {"expires_at": 999},
    {"params": {"message": ""}},
])
def test_peer_request_rejects_invalid_scope(updates):
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_request(_signed_peer(**updates), "req_a",
                                   token=TOKEN, now=1000)


def test_autonomous_context_cannot_claim_delegated_role():
    accepted = parse_trusted_peer_request(_signed_peer(origin_kind="agent_autonomous"),
                                          "req_a", token=TOKEN, now=1000)
    assert accepted["actor"] == "agent"
    with pytest.raises(TrustedContextError):
        parse_trusted_peer_request(
            _signed_peer(origin_kind="agent_autonomous", actor="admin"),
            "req_a", token=TOKEN, now=1000,
        )


def test_peer_cancellation_is_cleared_after_request():
    request_id = "cancelled-peer-test"
    clear_peer_coordination(request_id)
    assert not peer_coordination_cancelled(request_id)
    cancel_peer_coordination(request_id)
    assert peer_coordination_cancelled(request_id)
    clear_peer_coordination(request_id)
    assert not peer_coordination_cancelled(request_id)


def _card(unit_id, kind, skill_id, tags, action=""):
    property_name = "state" if kind == "mqtt" else "cmd"
    return {"unit_id": unit_id, "device_id": "esp32_desk" if unit_id == "esp32_desk_led" else unit_id,
            "online": True,
            "transport": {"kind": kind, "endpoint": "/api/go2/chat"},
            "skills": [{"id": skill_id, "tags": tags,
                        "params_schema": {"type": "object", "required": [property_name],
                                          "properties": {property_name: {"type": "string"}}},
                        "invoke": {"action": action}}]}








def test_dispatch_rejects_card_outside_configured_space(monkeypatch):
    from cloud.orchestrator.agentcore.tools import executor
    card = _card("esp32_desk_led", "mqtt", "set_light_state", ["light"], "SET_STATE")
    card["device_id"] = "neighbor_space_device"
    monkeypatch.setattr(executor, "get_space_registry", lambda: MagicMock(
        get_space=lambda: {"device_ids": ["esp32_desk", "go2"]},
    ))
    with pytest.raises(PermissionError, match="target_outside_space"):
        executor.validate_public_skill(card, card["skills"][0], {"state": "ON"},
                                       {"space_id": "main", "actor": "admin", "persist": False})
