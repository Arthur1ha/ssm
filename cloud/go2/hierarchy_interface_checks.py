"""Hierarchical state discovery/transport checks; no robot or model calls."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from cloud.cards.registry import CardRegistry
from cloud.cards.validation import normalize_card
from cloud.go2.actions import SPORT_COMMANDS


def _card():
    from cloud.go2.card import _go2_state_machine
    return {
        "card_schema_version": 1, "version": "2", "unit_id": "go2", "device_id": "go2",
        "name": "Go2", "description": "Robot", "agent_type": "robot",
        "transport": {"kind": "http", "endpoint": "/api/go2/chat"},
        "skills": [], "state_machine": _go2_state_machine(),
    }


def test_regions_preserve_flat_body_and_rest_observations():
    card = normalize_card(_card())
    graph = card["state_machine"]
    regions = {region["id"]: region for region in graph["regions"]}
    assert graph["state_field"] == "fsm_state"
    assert regions["body"]["state_field"] == "body_state"
    assert regions["body"]["states"] == graph["states"]
    assert regions["body"]["retain_when_inactive"] is True
    assert regions["life"]["parent"] == {"region": "activity", "state": "active"}
    assert regions["activity"]["parent"] == {"region": "connection", "state": "online"}
    assert graph["views"]["moving"]["label"] == "移动"
    # No dynamic value is baked into capability metadata.
    assert card["state"] == {}


@pytest.mark.parametrize("mutation", ["duplicate", "missing_parent", "cycle", "bad_edge", "bad_view"])
def test_invalid_optional_regions_are_rejected(mutation):
    card = _card()
    regions = card["state_machine"]["regions"]
    if mutation == "duplicate":
        regions.append(deepcopy(regions[0]))
    elif mutation == "missing_parent":
        regions[-1]["parent"] = {"region": "missing", "state": "active"}
    elif mutation == "cycle":
        regions[0]["parent"] = {"region": "life", "state": "idle"}
    elif mutation == "bad_edge":
        regions[1]["transitions"][0]["dst"] = "missing"
    else:
        regions[1]["views"]["active"] = {"type": "image", "src": "https://invalid.test/x"}
    with pytest.raises(ValueError):
        normalize_card(card)


def test_legacy_flat_cards_keep_working_without_regions():
    card = _card()
    del card["state_machine"]["regions"]
    assert "regions" not in normalize_card(card)["state_machine"]


def test_registry_rejects_old_revision_retired_epoch_and_legacy_updates():
    registry = CardRegistry()
    topic = "ssm/agents/go2/state"

    def send(epoch, revision, body):
        registry.handle_message(topic, json.dumps({
            "state_schema_version": 2, "unit_id": "go2", "epoch": epoch,
            "revision": revision, "body_state": body, "ts": 100,
        }), received_at=100)

    send("first", 20, "standing")
    send("first", 19, "lying")
    assert registry._states["go2"]["body_state"] == "standing"
    send("second", 0, None)
    send("first", 100, "lying")
    registry.handle_message(topic, json.dumps({"fsm_state": "standing"}))
    assert registry._states["go2"]["epoch"] == "second"
    assert registry._states["go2"]["body_state"] is None
    send("second", 1, "lying")
    assert registry._states["go2"]["body_state"] == "lying"


def test_mqtt_uses_same_snapshot_and_preserves_pending_and_gate(monkeypatch):
    from cloud.go2 import card
    snapshot = {
        "state_schema_version": 2, "epoch": "snapshot", "revision": 3,
        "connected": True, "connection_state": "online", "activity_state": "rest",
        "life_state": None, "body_state": "lying", "fsm_state": "lying",
        "available_actions": [], "available_life_actions": ["wake"],
        "life_blocked_reasons": {"request_rest": "already_resting"},
        "blocked_actions": {"StandUp": "resting"},
        "pending_action": {"command": "Sit", "phase": "accepted"}, "ts": 100,
    }
    mqtt = MagicMock()
    monkeypatch.setattr(card, "go2", SimpleNamespace(state_snapshot=lambda: snapshot))
    monkeypatch.setattr(card, "_mqtt_client", mqtt)
    card._publish_go2_state()
    args, kwargs = mqtt.publish.call_args
    assert args[0] == card.GO2_STATE_TOPIC
    payload = json.loads(args[1])
    for key, value in snapshot.items():
        assert payload[key] == value
    assert payload["unit_id"] == "go2"
    assert kwargs == {"retain": True, "qos": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["request_rest", "wake", "start_explore", "stop_explore"])
async def test_life_gateway_keeps_identity_and_normalizes_both_forms(monkeypatch, operation):
    from cloud.api import intents
    session = {"actor": "admin", "session_id": "identity-preserved"}
    dispatch = AsyncMock(return_value={"result": "ok"})
    monkeypatch.setattr(intents, "_internal_request", dispatch)
    await intents._dispatch_go2("LIFE", {"action": operation}, session)
    dispatch.assert_awaited_once_with("/api/go2/life", "POST", session, {"action": operation})
    dispatch.reset_mock()
    await intents._dispatch_go2(operation, {}, session)
    dispatch.assert_awaited_once_with("/api/go2/life", "POST", session, {"action": operation})


@pytest.mark.asyncio
async def test_life_gateway_keeps_admin_exploration_boundary_and_strict_params(monkeypatch):
    from cloud.api import intents
    dispatch = AsyncMock()
    monkeypatch.setattr(intents, "_internal_request", dispatch)
    visitor = {"actor": "visitor", "session_id": "visitor"}
    with pytest.raises(HTTPException) as error:
        await intents._dispatch_go2("LIFE", {"action": "start_explore"}, visitor)
    assert error.value.status_code == 403
    for action, params in [("wake", {"force": True}), ("LIFE", {"action": "invalid"}),
                           ("LIFE", {"action": "wake", "force": True}), ("LIFE", {"action": []}),
                           ("LIFE", {"action": {}})]:
        with pytest.raises(HTTPException):
            await intents._dispatch_go2(action, params, visitor)
    dispatch.assert_not_called()


@pytest.mark.parametrize("command", SPORT_COMMANDS)
def test_steward_delegation_can_discover_catalog_without_expanding_autonomy(command):
    from cloud.agents.steward.tools.coordination import allows_step
    task = {"unit_id": "go2", "skill_id": "go2_sport", "params": {"cmd": command}}
    assert allows_step(task, {}, "http", "user_delegated")
    assert allows_step(task, {}, "http", "agent_autonomous") is (command == "Hello")


@pytest.mark.parametrize("operation", ["request_rest", "wake", "start_explore", "stop_explore"])
def test_steward_delegation_exposes_life_without_autonomous_rule_entry(operation):
    from cloud.agents.steward.tools.coordination import allows_step
    task = {"unit_id": "go2", "skill_id": "go2_life", "params": {"action": operation}}
    assert allows_step(task, {}, "http", "user_delegated")
    assert not allows_step(task, {}, "http", "agent_autonomous")


def test_space_and_peer_projections_include_parallel_state():
    from cloud.peers.view import _state_summary
    from cloud.space.context import _summarize
    state = {"ts": 100, "connected": True, "connection_state": "online",
             "activity_state": "active", "life_state": "explore", "body_state": "standing",
             "control_mode": "normal", "autonomy_mode": "free_explore", "epoch": "epoch", "revision": 2}
    peer = _state_summary(state, 100)
    shared = _summarize("go2", {"state": state}, 100)["values"]
    for field in ("connection_state", "activity_state", "life_state", "body_state", "epoch", "revision"):
        assert peer[field] == shared[field] == state[field]


@pytest.mark.asyncio
async def test_busy_rest_gateway_does_not_cancel_activity_before_service_validation(monkeypatch):
    from cloud.api import activity, intents
    from fastapi import Response
    from cloud.peers import gateway
    dispatch = AsyncMock(return_value={"result": "blocked", "message": "生活状态忙碌"})
    intervention = AsyncMock()
    cancel = MagicMock()
    monkeypatch.setattr(intents, "_dispatch_go2", dispatch)
    monkeypatch.setattr(activity, "user_intervention", intervention)
    monkeypatch.setattr(gateway, "cancel_pending", cancel)
    session = {"actor": "visitor", "session_id": "visitor"}
    result = await intents.create_command("go2", intents.CommandRequest(
        action="request_rest"), Response(), session)
    assert result["result"]["result"] == "blocked"
    intervention.assert_not_called()
    cancel.assert_not_called()


def test_rest_peer_and_space_keep_null_life_region():
    from cloud.peers.view import _state_summary
    from cloud.space.context import _summarize
    state = {"ts": 100, "activity_state": "rest", "life_state": None, "body_state": "lying"}
    assert _state_summary(state, 100)["life_state"] is None
    assert _summarize("go2", {"state": state}, 100)["values"]["life_state"] is None


def test_public_life_skill_and_device_projection_retain_contract():
    from cloud.api.devices import _card_response
    from cloud.go2.card import get_card_skill
    from cloud.go2.tools.registry import get_tool_spec
    skill = get_card_skill("go2_life")
    assert skill["params_schema"] == get_tool_spec("go2_life").params_schema
    assert skill["invoke"] == {"action": "LIFE"}
    projection = _card_response(normalize_card(_card()))
    assert len(projection["state_machine"]["regions"]) == 4


def test_same_revision_heartbeat_refreshes_freshness_without_changing_facts():
    registry = CardRegistry()
    topic = "ssm/agents/go2/state"
    state = {"state_schema_version": 2, "unit_id": "go2", "epoch": "steady",
             "revision": 4, "body_state": "standing", "ts": 100}
    registry.handle_message(topic, json.dumps(state), received_at=100)
    registry.handle_message(topic, json.dumps({**state, "ts": 110}), received_at=110)
    assert registry._states["go2"]["_received_at"] == 110
    assert registry._states["go2"]["ts"] == 110
    registry.handle_message(topic, json.dumps({**state, "ts": 120, "body_state": "lying"}), received_at=120)
    registry.handle_message(topic, json.dumps(state), received_at=121)
    registry.handle_message(topic, json.dumps({**state, "ts": 122}), retained=True, received_at=122)
    assert registry._states["go2"]["body_state"] == "standing"
    assert registry._states["go2"]["_received_at"] == 110
