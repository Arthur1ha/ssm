"""The peer gateway must preserve authority and physical result semantics."""

import json
import time

from cloud.peers import gateway
from cloud.mqtt_signing import sign_message, verify_message
from cloud.space.execution import ExecutionContext


class _Cards:
    def __init__(self, *, lamp_online=True):
        self.cards = {
            "go2": {"unit_id": "go2", "device_id": "go2", "online": True,
                    "agent_type": "robot", "skills": []},
            "esp32_desk_led": {
                "unit_id": "esp32_desk_led", "device_id": "esp32_desk",
                "online": lamp_online, "agent_type": "actuator",
                "skills": [{"id": "set_light_state", "invoke": {"action": "SET_STATE"},
                            "params_schema": {"type": "object", "required": ["state"],
                                              "properties": {"state": {"enum": ["ON", "OFF"]}}}}],
            },
        }

    def get_card(self, unit_id):
        return self.cards.get(unit_id)


class _Space:
    def get_space(self):
        return {"device_ids": ["go2", "esp32_desk"]}


class _Mqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload), qos))
        return type("Info", (), {"rc": 0})()


class _State:
    def __init__(self, result="ok"):
        self.result = result
        self.waited = []

    def wait_task_result(self, task_id, timeout):
        self.waited.append(task_id)
        return {"task_id": task_id, "result": self.result}

    def get_task_result(self, task_id):
        return None


def _request(*, request_id="peer_test", context=None):
    return gateway.request_peer_skill(
        initiator_unit_id="go2", target_unit_id="esp32_desk_led",
        skill_id="set_light_state", params={"state": "ON"},
        context=context or ExecutionContext("ses_test", "visitor", False,
                                            expires_at=int(time.time()) + 60),
        origin_kind="user_delegated", request_id=request_id, timeout=0.01,
    )


def test_lamp_result_and_request_id_dedup(monkeypatch):
    mqtt, state = _Mqtt(), _State("ok")
    monkeypatch.setattr(gateway, "get_registry", lambda: _Cards())
    monkeypatch.setattr(gateway, "get_space_registry", lambda: _Space())
    gateway._requests.clear()
    gateway.init(mqtt, state)
    try:
        first = _request()
        second = _request()
        assert first["result"] == second["result"] == "ok"
        assert len(mqtt.published) == 1
        topic, payload, _ = mqtt.published[0]
        assert topic == "ssm/task/esp32_desk_led/peer_test"
        assert payload["task_id"] == payload["request_id"] == "peer_test"
        assert payload["actor"] == "visitor" and payload["persist"] is False
        assert state.waited == ["peer_test"]
    finally:
        gateway.shutdown()


def test_offline_and_expired_user_never_publish(monkeypatch):
    mqtt = _Mqtt()
    monkeypatch.setattr(gateway, "get_registry", lambda: _Cards(lamp_online=False))
    monkeypatch.setattr(gateway, "get_space_registry", lambda: _Space())
    gateway._requests.clear()
    gateway.init(mqtt, _State())
    try:
        assert _request(request_id="offline")["result"] == "blocked"
        expired = ExecutionContext("ses_old", "admin", True, expires_at=1)
        assert _request(request_id="expired", context=expired)["result"] == "blocked"
        assert mqtt.published == []
    finally:
        gateway.shutdown()


def test_timeout_is_cached_and_never_replayed(monkeypatch):
    mqtt = _Mqtt()
    monkeypatch.setattr(gateway, "get_registry", lambda: _Cards())
    monkeypatch.setattr(gateway, "get_space_registry", lambda: _Space())
    gateway._requests.clear()
    gateway.init(mqtt, _State("timeout"))
    try:
        assert _request(request_id="unknown")["result"] == "timeout"
        assert _request(request_id="unknown")["result"] == "timeout"
        assert len(mqtt.published) == 1
    finally:
        gateway.shutdown()


def test_late_esp32_result_can_be_queried_without_resending(monkeypatch):
    mqtt = _Mqtt()
    state = _State("timeout")
    state.get_task_result = lambda task_id: {"task_id": task_id, "result": "ok"}
    monkeypatch.setattr(gateway, "get_registry", lambda: _Cards())
    monkeypatch.setattr(gateway, "get_space_registry", lambda: _Space())
    gateway._requests.clear()
    gateway.init(mqtt, state)
    try:
        assert _request(request_id="late")["result"] == "timeout"
        assert gateway.get_request_result("late")["result"] == "ok"
        assert _request(request_id="late")["result"] == "ok"
        assert len(mqtt.published) == 1
    finally:
        gateway.shutdown()


def test_coordinator_result_requires_signature_and_updates_unknown_timeout(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token-123456")
    gateway._requests.clear()
    gateway._requests["peer_signed"] = gateway._Request("fingerprint", {
        "request_id": "peer_signed", "result": "timeout", "error": "coordinator_result_unknown",
        "initiator_unit_id": "go2", "target_unit_id": "cloud_orchestrator",
    })
    topic = "ssm/peer/result/peer_signed"
    forged = {"request_id": "peer_signed", "result": "ok",
              "initiator_unit_id": "go2", "target_unit_id": "cloud_orchestrator"}
    assert gateway.handle_peer_result(topic, json.dumps(forged)) is True
    assert gateway.get_request_result("peer_signed")["result"] == "timeout"
    signed = sign_message(topic, {"request_id": "peer_signed", "result": "blocked",
                                  "initiator_unit_id": "go2", "target_unit_id": "cloud_orchestrator"})
    assert gateway.handle_peer_result(topic, json.dumps(signed)) is True
    assert gateway.get_request_result("peer_signed")["result"] == "blocked"


def test_direct_user_takeover_signals_pending_coordinator(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token-123456")
    mqtt = _Mqtt()
    gateway._requests.clear()
    entry = gateway._Request("fingerprint", {
        "request_id": "peer_cancel", "result": "executing",
        "initiator_unit_id": "go2", "target_unit_id": "cloud_orchestrator",
        "origin_kind": "agent_autonomous", "origin_session_id": None,
    })
    entry.dispatched = True
    gateway._requests["peer_cancel"] = entry
    gateway.init(mqtt, _State())
    try:
        gateway.cancel_pending(origin_kind="agent_autonomous", reason="user_takeover")
        topic, payload, qos = mqtt.published[0]
        assert topic == "ssm/peer/cancel/peer_cancel" and qos == 1
        assert verify_message(topic, payload)["reason"] == "user_takeover"
        assert gateway.get_request_result("peer_cancel")["result"] == "executing"
    finally:
        gateway.shutdown()


def test_lamp_to_go2_uses_existing_http_skill_task(monkeypatch):
    cards = _Cards()
    cards.cards["go2"]["skills"] = [{
        "id": "go2_sport", "invoke": {"action": "SPORT"},
        "params_schema": {"type": "object", "required": ["cmd"],
                          "properties": {"cmd": {"enum": ["Hello"]}}},
    }]
    calls = []

    def fake_http(path, method, context, origin, request_id, timeout, body=None):
        calls.append((path, method, origin, request_id, body))
        return {"result": "ok", "task_id": request_id}

    monkeypatch.setattr(gateway, "get_registry", lambda: cards)
    monkeypatch.setattr(gateway, "get_space_registry", lambda: _Space())
    monkeypatch.setattr(gateway, "_go2_http", fake_http)
    gateway._requests.clear()
    gateway.init(_Mqtt(), _State())
    try:
        result = gateway.request_peer_skill(
            initiator_unit_id="esp32_desk_led", target_unit_id="go2",
            skill_id="go2_sport", params={"cmd": "Hello"},
            context=ExecutionContext("ses_lamp", "visitor", False,
                                     expires_at=int(time.time()) + 60),
            origin_kind="user_delegated", request_id="peer_hello",
        )
        assert result["result"] == "ok"
        assert calls[0][0:4] == ("/api/go2/chat", "POST", "user_delegated", "peer_hello")
        assert calls[0][4]["skill_id"] == "go2_sport"
        assert calls[0][4]["params"] == {"cmd": "Hello"}
        assert calls[0][4]["task_id"] == "peer_hello"
    finally:
        gateway.shutdown()


def test_go2_http_uses_trusted_headers_without_leaking_token_into_body(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token-123456")
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"result":"ok"}'

    class _Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = {key.lower(): value for key, value in request.header_items()}
            captured["body"] = json.loads(request.data)
            return _Response()

    monkeypatch.setattr(gateway.urllib.request, "build_opener", lambda *_args: _Opener())
    context = ExecutionContext("ses_lamp", "visitor", False,
                               expires_at=int(time.time()) + 60)
    result = gateway._go2_http("/api/go2/chat", "POST", context,
                               "user_delegated", "peer_http", 2.0,
                               {"task_id": "peer_http"})
    assert result["result"] == "ok"
    assert captured["headers"]["x-ssm-service-token"] == "test-service-token-123456"
    assert captured["headers"]["x-ssm-origin-kind"] == "user_delegated"
    assert "test-service-token-123456" not in json.dumps(captured["body"])


def test_go2_http_refuses_redirect_with_service_headers(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token-123456")

    def check_opener(*handlers):
        redirect_handlers = [handler for handler in handlers
                             if isinstance(handler, gateway.urllib.request.HTTPRedirectHandler)]
        assert len(redirect_handlers) == 1
        request = gateway.urllib.request.Request("http://127.0.0.1:8082/api/go2/chat")
        assert redirect_handlers[0].redirect_request(
            request, None, 302, "redirect", {}, "https://outside.example/collect"
        ) is None

        class _Opener:
            def open(self, request, timeout):
                raise gateway.urllib.error.HTTPError(request.full_url, 302, "redirect", {}, None)

        return _Opener()

    monkeypatch.setattr(gateway.urllib.request, "build_opener", check_opener)
    context = ExecutionContext("ses_lamp", "visitor", False,
                               expires_at=int(time.time()) + 60)
    result = gateway._go2_http("/api/go2/chat", "POST", context,
                               "user_delegated", "peer_redirect", 2.0,
                               {"task_id": "peer_redirect"})
    assert result["result"] == "error"
