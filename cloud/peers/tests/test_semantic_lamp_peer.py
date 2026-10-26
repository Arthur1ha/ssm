"""A semantic lamp peer request is single-shot and result-aware."""

import time

import pytest

from cloud.peers import gateway
from cloud.space.execution import ExecutionContext


def test_semantic_lamp_query_keeps_original_request_id(monkeypatch):
    calls = []
    answers = iter([{"result": "executing"}, {"result": "ok", "physical_task_id": "edge_1"}])
    def fake_http(path, method, context, origin, request_id, timeout, body=None):
        calls.append((path, method, request_id, body))
        return next(answers)
    monkeypatch.setattr(gateway, "_go2_http", fake_http)
    monkeypatch.setattr(gateway.time, "sleep", lambda _: None)
    context = ExecutionContext("s1", "admin", False, "main", "user_delegated",
                               int(time.time()) + 30)
    result = gateway._dispatch_lamp_semantic(
        "request_1", "esp32_desk_led",
        "/api/internal/esp32/esp32_desk_led/agent/tasks",
        {"goal": "柔和一点"}, context, "user_delegated", 1,
    )
    assert result["result"] == "ok"
    assert [call[1] for call in calls] == ["POST", "GET"]
    assert calls[0][3]["task_id"] == "request_1"
    assert calls[1][0].endswith("/request_1")


def test_autonomous_semantic_request_is_denied():
    with pytest.raises(PermissionError):
        gateway._skill_policy("go2", "esp32_desk_led", "adjust_lighting",
                              {"goal": "柔和一点"}, "agent_autonomous")
