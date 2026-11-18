import asyncio
import json
import pytest
from fastapi import HTTPException

from cloud.api import intents
from cloud.cards.registry import get_registry
from cloud.mqtt_signing import verify_message
from edge.lamp_contract import FSM_VERSION, capabilities

from .conftest import login_admin


@pytest.mark.asyncio
async def test_intent_gets_server_owned_context_and_signed_envelope(client, api_env):
    _store, mqtt = api_env
    csrf = await login_admin(client)
    response = await client.post(
        "/api/intents", json={"text": "开灯", "intent_hint": "act"}, headers=csrf,
    )
    assert response.status_code == 202
    topic, raw, _kwargs = mqtt.messages[-1]
    body = json.loads(raw)
    assert topic == f"ssm/intent/{body['context']['session_id']}"
    assert body["text"] == "开灯"
    assert body["context"]["actor"] == "admin"
    assert body["context"]["persist"] is True
    assert body["context"]["space_id"] == "main"
    assert "internal_token" not in body
    assert body["_mqtt_auth"]["alg"] == "HMAC-SHA256"
    unsigned = verify_message(topic, body, internal_token="i" * 32)
    assert unsigned["text"] == "开灯"
    from cloud.space.user_goals import get_goal_registry
    assert unsigned["goal_revision"] == get_goal_registry().current()["revision"]
    assert unsigned["context"]["goal_revision"] == unsigned["goal_revision"]
    assert "user_msg" not in body


@pytest.mark.asyncio
async def test_graph_clarification_resume_keeps_original_trace(client, api_env, monkeypatch):
    _store, mqtt = api_env
    csrf = await login_admin(client)
    from cloud.agents.common.runner import GraphRun
    from cloud.agents.common import views
    from cloud.space.user_goals import get_goal_registry

    def graph_run(run_id, session_id, agent_id):
        assert run_id == "evt_graph_clarification"
        assert session_id
        assert agent_id == "cloud_orchestrator"
        return GraphRun(run_id, session_id, "原问题", 9999999999,
                        status="needs_clarification", trace_id="tr_original_graph",
                        goal_revision=get_goal_registry().current()["revision"])

    monkeypatch.setattr(views, "load_run", graph_run)
    response = await client.post(
        "/api/intents", json={"text": "客厅", "resume_run_id": "evt_graph_clarification"},
        headers=csrf,
    )
    assert response.status_code == 202
    topic, raw, _kwargs = mqtt.messages[-1]
    payload = verify_message(topic, json.loads(raw), internal_token="i" * 32)
    assert payload["resume_run_id"] == "evt_graph_clarification"
    assert payload["trace_id"] == "tr_original_graph"
    assert payload["goal_revision"] == get_goal_registry().current()["revision"]


@pytest.mark.asyncio
async def test_client_cannot_forge_context(client):
    csrf = await login_admin(client)
    response = await client.post(
        "/api/intents",
        json={"text": "hello", "actor": "visitor", "persist": False, "space_id": "evil",
              "goal_revision": 123},
        headers=csrf,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_go2_command_uses_trusted_internal_headers(client, monkeypatch):
    csrf = await login_admin(client)
    captured = {}

    async def fake_request(path, method, session, body=None):
        captured.update(path=path, method=method, session=session, body=body)
        return {"status": "ok"}

    monkeypatch.setattr(intents, "_internal_request", fake_request)
    response = await client.post(
        "/api/commands/go2",
        json={"action": "velocity", "params": {"vx": 0.2, "vy": 0, "vyaw": 0,
                                                   "controller_id": "gesture-one", "sequence": 1}},
        headers=csrf,
    )
    assert response.status_code == 200
    assert captured["path"] == "/api/go2/velocity"
    assert captured["session"]["actor"] == "admin"
    from cloud.space.user_goals import get_goal_registry
    assert captured["session"]["goal_revision"] == get_goal_registry().current()["revision"]
    assert captured["body"]["vx"] == 0.2


@pytest.mark.asyncio
async def test_go2_chat_task_id_is_forwarded_and_query_uses_session(client, monkeypatch):
    csrf = await login_admin(client)
    calls = []

    async def fake_request(path, method, session, body=None):
        calls.append((path, method, session["session_id"], body))
        return {"result": "executing", "task_id": "go2_test_1"}

    monkeypatch.setattr(intents, "_internal_request", fake_request)
    response = await client.post(
        "/api/commands/go2", json={"action": "chat", "params": {
            "message": "前进两秒", "task_id": "go2_test_1",
        }}, headers=csrf,
    )
    assert response.status_code == 202
    assert response.json()["result"]["task_id"] == "go2_test_1"
    assert calls[0][0:2] == ("/api/go2/chat", "POST")
    assert calls[0][3]["task_id"] == "go2_test_1"
    assert calls[0][3]["deadline"] > 0

    query = await client.get("/api/commands/go2/tasks/go2_test_1")
    assert query.status_code == 200
    assert calls[1][0:2] == ("/api/go2/tasks/go2_test_1", "GET")
    assert calls[1][2] == calls[0][2]


@pytest.mark.asyncio
async def test_state_machine_emergency_stop_uses_safety_endpoint(client, monkeypatch):
    csrf = await login_admin(client)
    captured = {}

    async def fake_request(path, method, session, body=None):
        captured.update(path=path, method=method, actor=session["actor"])
        return {"ok": True, "mode": "manual"}

    monkeypatch.setattr(intents, "_internal_request", fake_request)
    response = await client.post(
        "/api/commands/go2", json={"action": "STOP", "params": {}}, headers=csrf,
    )
    assert response.status_code == 200
    assert captured == {"path": "/api/go2/stop", "method": "POST", "actor": "admin"}
    assert response.json()["result"]["ok"] is True




@pytest.mark.asyncio
async def test_visitor_go2_asset_command_is_rejected_before_dispatch(monkeypatch):
    called = False

    async def fake_request(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(intents, "_internal_request", fake_request)
    with pytest.raises(HTTPException) as exc_info:
        await intents._dispatch_go2(
            "tag_location", {"name": "门口"}, {"actor": "visitor", "session_id": "ses_v"}
        )
    assert exc_info.value.status_code == 403
    assert called is False


@pytest.mark.asyncio
async def test_esp32_command_has_server_session_for_edge_correlation(client, api_env):
    api_env[0].update_space(device_ids=["esp32_desk"])
    _store, mqtt = api_env
    get_registry().handle_message(
        "ssm/agents/esp32_desk_led/manifest",
        json.dumps({
            "unit_id": "esp32_desk_led", "parent_id": "esp32_desk",
            "agent_type": "actuator", "name": "led", "hw_platform": "esp32",
            "firmware_ver": "test", "fsm_version": FSM_VERSION,
            "capabilities": capabilities(),
        }),
    )
    get_registry().handle_message("ssm/agents/esp32_desk/status", "online")
    get_registry().handle_message("ssm/agents/esp32_desk_led/state", json.dumps({"ism": "OFF"}))
    csrf = await login_admin(client)
    response = await client.post(
        "/api/commands/esp32_desk_led",
        json={"action": "SET_COLOR", "params": {"r": 1, "g": 2, "b": 3, "brightness": 120}},
        headers=csrf,
    )
    assert response.status_code == 202
    body = json.loads(mqtt.messages[-1][1])
    assert body["session_id"]
    assert body["space_id"] == "main"
    assert body["actor"] == "admin"
    assert body["persist"] is True
    assert isinstance(body["ts"], int)
    assert "context" not in body
    assert "internal_token" not in body


def test_feedback_is_routed_only_to_its_session():
    q1 = intents._events.subscribe("ses_a")
    q2 = intents._events.subscribe("ses_b")
    try:
        assert intents.handle_mqtt_message("ssm/feedback/ses_a", '{"stage":"done","text":"ok"}')
        assert q1.get_nowait()["text"] == "ok"
        assert q2.empty()
    finally:
        intents._events.unsubscribe("ses_a", q1)
        intents._events.unsubscribe("ses_b", q2)


def test_device_event_is_broadcast_to_active_sessions(api_env):
    api_env[0].update_space(device_ids=["go2"])
    q1 = intents._events.subscribe("ses_a")
    q2 = intents._events.subscribe("ses_b")
    try:
        assert intents.handle_mqtt_message("ssm/agents/go2/status", "offline")
        assert q1.get_nowait()["online"] is False
        assert q2.get_nowait()["type"] == "device"
    finally:
        intents._events.unsubscribe("ses_a", q1)
        intents._events.unsubscribe("ses_b", q2)


@pytest.mark.asyncio
async def test_expired_session_watcher_triggers_resource_cleanup(monkeypatch):
    cleaned = []

    monkeypatch.setattr(intents, "session_is_active", lambda _session: False)

    async def fake_end_session(session, reason):
        cleaned.append((session["session_id"], reason))

    monkeypatch.setattr(intents, "end_session", fake_end_session)
    session = {
        "session_id": "ses_expired",
        "actor": "visitor",
        "issued_at": 1,
        "expires_at": 2,
    }
    intents.schedule_session_cleanup(session)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cleaned == [("ses_expired", "expired_or_revoked")]
    assert "ses_expired" not in intents._cleanup_tasks


@pytest.mark.asyncio
async def test_session_end_mqtt_event_is_signed_without_service_token(
    api_env, monkeypatch
):
    _store, mqtt = api_env
    session = {
        "session_id": "ses_cleanup_signed",
        "actor": "visitor",
        "issued_at": 100,
        "expires_at": 200,
    }
    monkeypatch.setattr(
        "cloud.go2.router.cleanup_session_resources", lambda _session_id: None
    )
    intents._ended_sessions.pop(session["session_id"], None)

    await intents.end_session(session, reason="logout")

    topic, raw, _kwargs = mqtt.messages[-1]
    body = json.loads(raw)
    assert topic == "ssm/session/ses_cleanup_signed/ended"
    assert "internal_token" not in body
    unsigned = verify_message(topic, body, internal_token="i" * 32)
    assert unsigned["reason"] == "logout"
    assert unsigned["context"]["session_id"] == session["session_id"]
