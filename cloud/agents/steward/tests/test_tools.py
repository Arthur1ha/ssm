"""Steward Card and permission gates around native tool declarations."""

from pydantic import ValidationError
import pytest

from cloud.agents.steward import tools
from cloud.agents.common.runner import GraphRun
from cloud.peers.cancellation import cancel_peer_coordination, clear_peer_coordination
import asyncio
import time


def test_invoke_skill_rechecks_card_and_peer_policy(monkeypatch):
    dispatched = []
    card = {"online": True, "skills": [{"id": "greet", "invoke": {"kind": "http"}}]}
    monkeypatch.setattr(tools, "_cards", lambda _context: {"go2": card})
    monkeypatch.setattr(tools, "skill_requires_persistence", lambda _skill: False)
    monkeypatch.setattr(tools, "validate_public_skill", lambda *_args: {"kind": "http", "endpoint": "/api/go2/greet"})
    monkeypatch.setattr(tools, "allows_step", lambda *_args: False)
    monkeypatch.setattr(tools.transport, "do_http_dispatch", lambda *_args: dispatched.append(1))
    context = {"session_id": "peer-1", "origin_kind": "agent_autonomous", "persist": False}

    result = tools._invoke_skill(context, {"unit_id": "go2", "skill_id": "greet", "params": {}}, "step-1", 9999999999)
    assert result == {"result": "blocked", "error": "coordination_skill_denied"}
    assert dispatched == []


def test_native_tool_schema_requires_skill_target():
    schema = tools.STEWARD_TOOL_SCHEMAS["invoke_skill"]
    with pytest.raises(ValidationError):
        schema.model_validate({"skill_id": "greet", "params": {}})


def test_invoke_skill_uses_public_card_http_endpoint(monkeypatch):
    calls = []
    card = {"online": True, "skills": [{"id": "greet", "invoke": {"kind": "http"}}]}
    monkeypatch.setattr(tools, "_cards", lambda _context: {"go2": card})
    monkeypatch.setattr(tools, "skill_requires_persistence", lambda _skill: False)
    monkeypatch.setattr(tools, "validate_public_skill", lambda *_args: {"kind": "http", "endpoint": "/api/go2/greet"})
    monkeypatch.setattr(tools, "allows_step", lambda *_args: True)
    monkeypatch.setattr(tools.transport, "do_http_dispatch", lambda endpoint, body, timeout: (
        calls.append((endpoint, body, timeout)) or {"result": "ok", "task_id": body["task_id"]}))
    context = {"session_id": "session-1", "origin_kind": "user_delegated", "persist": False,
               "actor": "admin", "space_id": "default"}

    result = tools._invoke_skill(context, {"unit_id": "go2", "skill_id": "greet", "params": {}}, "step-1", 9999999999)
    assert result["result"] == "ok"
    assert result["task_id"] == "step-1"
    assert len(calls) == 1
    assert calls[0][0] == "/api/go2/greet"


def test_peer_cancellation_reaches_graph_executor():
    request_id = "cancel-shared-test"
    clear_peer_coordination(request_id)
    try:
        cancel_peer_coordination(request_id)
        assert tools.peer_coordination_cancelled(request_id)
        run = GraphRun("run", "peer-session", "查看状态", time.time() + 10)
        execute = tools.make_executor(run, {"peer_request_id": request_id})
        result = asyncio.run(execute("read_state", {}, "read-1"))
        assert result == {"result": "cancelled", "error": "higher_priority_user_request"}
    finally:
        clear_peer_coordination(request_id)
