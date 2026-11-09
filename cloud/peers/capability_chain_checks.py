"""Public skill resolution, policy and ordered physical-result integration."""

import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.common.runner import GraphRun, run_role_graph, invoke_blocking
from cloud.agents.go2 import graph
from cloud.agents.go2.run_view import Go2RunView
from cloud.agents.steward import tools as steward
from cloud.esp32 import physical_tasks
from cloud.esp32.state import ESP32State
from cloud.peers import gateway
from cloud.peers.policy import permits_skill
from cloud.space.execution import ExecutionContext


def _evidence(name, payload):
    """Optionally export simulated acceptance evidence without production I/O."""
    import json
    import os
    from pathlib import Path
    if directory := os.getenv("SSM_CAPABILITY_EVIDENCE_DIR"):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / (name + ".json")).write_text(json.dumps(
            {"simulated": True, **payload}, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize('skill_id,params', [
    ('go2_find_object', {'target': '白色猫头鹰'}),
])
def test_steward_search_preserves_child_budget(monkeypatch, skill_id, params):
    from cloud.go2.card import _build_go2_card
    from cloud.agents.go2.workflows.search import SEARCH_SERVER_TIMEOUT_S
    monkeypatch.setattr(steward, '_cards', lambda _: {'go2': {**_build_go2_card(), 'online': True}})
    captured = []
    def dispatch(endpoint, body, timeout):
        captured.append(body)
        return {'result': 'blocked', 'reason': 'simulated'}
    monkeypatch.setattr(steward.transport, 'do_http_dispatch', dispatch)
    now = time.time()
    context = {'session_id': 'user', 'actor': 'visitor', 'persist': False,
               'origin_kind': 'user_delegated', 'expires_at': now + 600}
    result = steward._invoke_skill(context, {'unit_id': 'go2', 'skill_id': skill_id,
        'params': params}, 'search-child', now + 300)
    assert result['result'] == 'blocked'
    assert now + SEARCH_SERVER_TIMEOUT_S <= captured[0]['deadline'] <= time.time() + SEARCH_SERVER_TIMEOUT_S


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_id,params,expected_tool,goal", [
    ("go2_chat", {"message": "你好"}, None, "你好"),
    ("go2_sport", {"cmd": "Hello"}, ("go2_sport", {"cmd": "Hello"}), "公开请求"),
    ("go2_life", {"action": "wake"}, ("go2_life", {"action": "wake"}), "公开请求"),
])
async def test_public_entry_resolves_conversation_and_fixed_tools(
        monkeypatch, tmp_path, skill_id, params, expected_tool, goal):
    view = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                      index=FileRunIndex(tmp_path / "index"))
    monkeypatch.setattr(graph, "get_store", lambda: view)
    monkeypatch.setattr(graph, "runtime_for", lambda _robot: None)
    monkeypatch.setattr(graph, "get_text_llm", lambda: object())
    monkeypatch.setattr(graph, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.api.intents.publish_agent_event", lambda *_a, **_k: {})
    monkeypatch.setattr(view, "pin", lambda *_a: None)
    calls = []

    async def run(current, **kwargs):
        calls.append((current.goal, kwargs["initial_tool"]))
        current.status = "ok"

    monkeypatch.setattr(graph, "run_role_graph", run)
    context = ExecutionContext("public-entry", "visitor", False, expires_at=int(time.time()) + 60)
    result = await graph.run_go2_loop(context.session_id, "公开请求", context=context,
                                      skill_id=skill_id, params=params, publish_reply=False)
    assert result["result"] == "ok"
    assert calls == [(goal, expected_tool)]


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_id,params,error", [
    ("go2_private", {}, "unknown_public_skill"),
    ("go2_chat", {"message": ""}, "invalid_skill_parameters"),
    ("go2_chat", {"message": "hello", "actor": "admin"}, "invalid_skill_parameters"),
    ("go2_sport", {"cmd": "UnsupportedFirmwareAction"}, "invalid_skill_parameters"),
    ("go2_move", {"direction": "forward", "duration": 3.3}, "invalid_skill_parameters"),
])
async def test_invalid_public_requests_never_register_or_dispatch(monkeypatch, skill_id, params, error):
    class UnusedView:
        def get(self, *_a, **_k):
            pytest.fail("invalid public request registered a run")

    result = await graph._run_go2_loop_locked(
        "public-invalid", "request", context=ExecutionContext("public-invalid", "visitor", False),
        view=UnusedView(), skill_id=skill_id, params=params, run_id=None, deadline=None,
        on_step=None, resume=False, trace_id=None, parent_run_id=None,
        allowed_tools=None, tool_guard=None, task_context=None, publish_reply=False, selection=None)
    assert result["result"] == "rejected"
    assert result["error"] == error


def test_shared_policy_preserves_explicit_authority_differences():
    for audience in ("peer", "steward"):
        assert permits_skill("go2", "go2_sport", {"cmd": "Hello"}, "user_delegated", audience=audience)
        assert not permits_skill("go2", "go2_move", {"direction": "forward", "distance_m": 1},
                                 "agent_autonomous", audience=audience)
        assert not permits_skill("go2", "go2_chat", {"message": "walk"},
                                 "agent_autonomous", audience=audience)
    assert permits_skill("go2", "go2_life", {"action": "wake"}, "user_delegated", audience="steward")
    assert not permits_skill("go2", "go2_life", {"action": "wake"}, "user_delegated", audience="peer")
    assert not permits_skill("go2", "go2_sport", {"cmd": "Dance1"}, "agent_autonomous", audience="peer")
    assert permits_skill("go2", "go2_sport", {"cmd": "Dance1"}, "agent_autonomous",
                         audience="peer", activity_authorized=True)


def _lamp_card():
    from cloud.cards.builder import build_card_from_manifest
    from edge.lamp_contract import capabilities, FSM_VERSION
    card = build_card_from_manifest({'firmware_ver': '0.3.0',
        "unit_id": "esp32_desk_led", "parent_id": "esp32_desk", "name": "灯",
        "agent_type": "actuator", "fsm": "led", "fsm_version": FSM_VERSION,
        "capabilities": capabilities()})
    return {**card, "online": True}


def test_peer_lamp_reuses_physical_boundary_and_durable_receipt(monkeypatch, tmp_path):
    import json
    state = ESP32State()
    state.on_manifest("esp32_desk_led", {"agent_type": "actuator"})
    state.on_agent_msg("esp32_desk_led", "state", {"ism": "ON", "brightness": 120})
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    card = _lamp_card()
    monkeypatch.setattr(gateway, "get_registry", lambda: SimpleNamespace(get_card=lambda _uid: card))
    publications = []

    def publish(topic, raw, **_kwargs):
        payload = json.loads(raw)
        publications.append(payload)
        state.store_task_result(payload["task_id"], {"result": "ok", "task_id": payload["task_id"],
                                                   "unit_id": "esp32_desk_led", "ism": "ON"})
        return SimpleNamespace(rc=0)

    monkeypatch.setattr(gateway, "_mqtt", SimpleNamespace(publish=publish))
    monkeypatch.setattr(gateway, "_esp32_state", state)
    context = ExecutionContext("peer-session", "visitor", False, expires_at=int(time.time()) + 60)
    args = ("peer-blink", "esp32_desk_led", "BLINK", {"count": 2}, context,
            "visitor", "user_delegated", 1)
    first = gateway._dispatch_lamp(*args)
    second = gateway._dispatch_lamp(*args)
    assert first["result"] == second["result"] == "ok"
    assert len(publications) == 1
    assert publications[0]["task_id"] == publications[0]["request_id"] == "peer-blink"
    conflict = gateway._dispatch_lamp(*(*args[:3], {"count": 3}, *args[4:]))
    assert conflict["result"] == "rejected"
    assert len(publications) == 1
    _evidence("lamp-retry-and-rejection", {
        "published": publications, "first": first, "retry": second, "conflict": conflict})


@pytest.mark.asyncio
@pytest.mark.parametrize("lamp_result", ["ok", "timeout"])
async def test_steward_waits_for_blink_receipt_before_go2(monkeypatch, tmp_path, lamp_result):
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.agents.steward.tools import card as card_validation
    from cloud.go2.card import _build_go2_card
    from cloud.orchestrator.shared_state import SharedState
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path / "physical")
    lamp = _lamp_card()
    dog = {**_build_go2_card(), "online": True}
    cards = {"esp32_desk_led": lamp, "go2": dog}
    state = SharedState()
    state.on_manifest("esp32_desk_led", {"agent_type": "actuator"})
    state.on_agent_msg("esp32_desk_led", "state", {"ism": "ON", "brightness": 120})
    monkeypatch.setattr(steward.transport, "_state", state)
    monkeypatch.setattr(steward.transport, "_registry", SimpleNamespace(get_all_cards=lambda: cards))
    monkeypatch.setattr(card_validation, "get_space_registry", lambda: SimpleNamespace(
        get_space=lambda: {"device_ids": ["esp32_desk", "go2"]}))
    sequence = []

    def publish(uid, task_id, action, params, session_id, **_kwargs):
        sequence.append("blink_dispatched")

    def receipt(task_id, session_id, deadline, **_kwargs):
        sequence.append("blink_" + lamp_result)
        return {"task_id": task_id, "result": lamp_result, "ism": "ON"}

    def http(endpoint, body, timeout):
        if endpoint.endswith("/agent/replies"):
            return {}
        assert endpoint == "/api/go2/chat"
        assert body["skill_id"] == "go2_sport" and body["params"] == {"cmd": "Hello"}
        assert sequence[-1] == "blink_ok"
        sequence.append("hello_completed")
        return {"result": "ok", "task_id": body["task_id"], "confirmation": "simulated_joint_posture"}

    monkeypatch.setattr(steward.transport, "do_publish_task", publish)
    monkeypatch.setattr(steward.transport, "do_http_dispatch", http)
    monkeypatch.setattr(steward, "_wait_mqtt_result", receipt)
    context = {"session_id": "sequence-session", "actor": "visitor", "persist": False,
               "space_id": "main", "origin_kind": "user_delegated", "expires_at": time.time() + 60}
    choices = [
        {"unit_id": "esp32_desk_led", "skill_id": "blink_light", "params": {"count": 2}},
        {"unit_id": "go2", "skill_id": "go2_sport", "params": {"cmd": "Hello"}},
    ]

    class Model:
        def bind_tools(self, _tools, **kwargs):
            assert kwargs["parallel_tool_calls"] is False
            return self

        async def ainvoke(self, _messages):
            if choices:
                return AIMessage(content="", tool_calls=[{
                    "name": "invoke_skill", "args": choices.pop(0), "id": "call-" + str(len(choices)),
                }])
            return AIMessage(content="完成")

    run = GraphRun("sequence-run", context["session_id"], "先闪灯两次再挥手", time.time() + 30,
                   agent_id="cloud_orchestrator", origin_kind="user_delegated")
    async def execute(_name, arguments, step_id):
        return await invoke_blocking(steward._invoke_skill, context, arguments, step_id, run.deadline)

    await run_role_graph(run, model=Model(), tool_schemas={"invoke_skill": {
        "name": "invoke_skill", "description": "公开技能", "parameters": {
            "type": "object", "properties": {"unit_id": {"type": "string"},
                "skill_id": {"type": "string"}, "params": {"type": "object"}},
            "required": ["unit_id", "skill_id", "params"]}}},
        build_messages=lambda _run: [HumanMessage(content="先闪灯，再挥手")], execute=execute,
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert sequence == (["blink_dispatched", "blink_ok", "hello_completed"] if lamp_result == "ok"
                        else ["blink_dispatched", "blink_timeout"])
    assert run.status == ("ok" if lamp_result == "ok" else "timeout")
    _evidence("blink-then-hello-" + lamp_result, {
        "sequence": sequence, "result": run.status, "steps": run.steps})


@pytest.mark.asyncio
async def test_page_lamp_retry_and_rejection_do_not_cancel_new_activity(monkeypatch, tmp_path):
    from functools import partial
    from fastapi import HTTPException, Response
    from cloud.api import intents, main, activity
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    state = ESP32State()
    state.on_manifest("esp32_desk_led", {"agent_type": "actuator"})
    state.on_agent_msg("esp32_desk_led", "state", {"ism": "ON", "brightness": 120})
    monkeypatch.setattr(main, "_esp32_state", state)
    monkeypatch.setattr(intents, "_configured_card", lambda _unit: _lamp_card())
    calls = []

    async def takeover():
        calls.append("takeover")

    async def in_worker(fn, *args, **kwargs):
        return await invoke_blocking(partial(fn, *args, **kwargs))

    def dispatch(_uid, _action, _params, _session, **kwargs):
        calls.append("publish")
        return kwargs["task_id"]

    monkeypatch.setattr(activity, "user_intervention", takeover)
    monkeypatch.setattr(gateway, "cancel_pending", lambda **_kwargs: None)
    monkeypatch.setattr(intents, "_dispatch_mqtt_task", dispatch)
    monkeypatch.setattr(intents, "run_in_threadpool", in_worker)
    session = {"session_id": "page", "actor": "visitor", "expires_at": time.time() + 60}
    request = intents.CommandRequest(action="BLINK", params={"count": 2}, request_id="click-one")
    first = await intents.create_command("esp32_desk_led", request, Response(), session)
    second = await intents.create_command("esp32_desk_led", request, Response(), session)
    assert first["result"] == "accepted" and first["accepted"] is True
    assert second["result"] == "unknown" and second["accepted"] is False
    assert first["task_id"] == second["task_id"]
    assert calls == ["takeover", "publish"]
    with pytest.raises(HTTPException):
        await intents.create_command("esp32_desk_led", intents.CommandRequest(
            action="BLINK", params={"count": -1}), Response(), session)
    assert calls == ["takeover", "publish"]


@pytest.mark.asyncio
async def test_page_go2_forwards_correlation_without_early_takeover(monkeypatch):
    from fastapi import Response
    from cloud.api import intents, activity
    forwarded = []
    async def dispatch(path, method, session, body=None):
        forwarded.append((path, method, body))
        return {"result": "accepted", "command_request_id": body["request_id"]}
    async def premature():
        pytest.fail("gateway cancelled activity before device validation and deduplication")
    monkeypatch.setattr(intents, "_internal_request", dispatch)
    monkeypatch.setattr(intents, "ensure_go2_action", lambda _action: None)
    monkeypatch.setattr(activity, "user_intervention", premature)
    request = intents.CommandRequest(action="SPORT", params={"cmd": "Hello"},
                                     request_id="button-one", trace_id="tr_" + "a" * 32,
                                     deadline=time.time() + 30)
    result = await intents.create_command("go2", request, Response(), {"session_id": "page", "actor": "visitor"})
    assert result["result"]["command_request_id"] == "button-one"
    assert forwarded == [("/api/go2/commands", "POST", {
        "cmd": "Hello", "params": {}, "request_id": "button-one",
        "trace_id": request.trace_id, "deadline": request.deadline})]


def test_steward_restart_reads_durable_receipt_without_republishing(monkeypatch, tmp_path):
    monkeypatch.setattr(physical_tasks, "PHYSICAL_TASKS_DIR", tmp_path)
    monkeypatch.setattr(steward.transport, "_state", None)
    physical_tasks.reserve("step-one", unit_id="esp32_desk_led", session_id="owner",
                           action="BLINK", params={"count": 2})
    physical_tasks.record_result("step-one", "esp32_desk_led", {"result": "ok", "ism": "ON"})
    result = steward._wait_mqtt_result("step-one", "owner", time.time() + 1,
                                       unit_id="esp32_desk_led")
    assert result["result"] == "ok"
