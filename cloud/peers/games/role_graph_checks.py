"""A few offline boundary checks for games using the original device brains."""
from __future__ import annotations

import time
import threading
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from cloud.agents.go2 import graph as go2_graph
from cloud.agents.go2.tests.test_graph_entry import ScriptedModel, entry
from cloud.agents.lamp.state_context_checks import facts
from cloud.peers.games.role_graph import DECISION_TOOL, RoleTask, decide, run_device_task
from cloud.space.execution import ExecutionContext


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, "深蓝色小盒", "另一个箱子"])
async def test_delivery_search_uses_bound_target_before_checkpoint_and_dispatch(entry, monkeypatch, target):
    from cloud.peers.games.push_delivery import cargo_search_schema
    cargo = "画面中央偏下、距你约1.5–2米地面上平放的深蓝色小盒，正面有白色圆形图案"
    model = ScriptedModel([AIMessage(content="", tool_calls=[{
        "name": "go2_find_object", "id": "find-order-cargo",
        "args": {} if target is None else {"target": target}}])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "runtime_for", lambda _: None)
    monkeypatch.setattr(go2_graph, "go2", SimpleNamespace(is_connected=True, state_snapshot=lambda: {}))
    execute = AsyncMock(return_value={"result": "ok", "target": cargo,
        "outcome": "nearby", "found": True, "stop": {"result": "ok"}})
    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("peer_delivery_bound", "visitor", False,
                               origin_kind="agent_autonomous", expires_at=int(time.time() + 60))
    result = await run_device_task("go2", "寻找本单货物。", context=context,
        run_id="bound-cargo", deadline=time.time() + 50,
        allowed_tools={"go2_find_object", "go2_observe"},
        tool_declarations={"go2_find_object": cargo_search_schema(cargo)},
        result_transform=lambda _n, _p, r: {**r, "final": True},
        share_result=False, reply_required=False)
    assert result["result"] == "ok", result
    assert execute.await_args.args == ("go2_find_object", {"target": cargo})
    assert result["tool_results"][0]["params"] == {"target": cargo}
    saved = go2_graph.get_store().get("bound-cargo", context.session_id)
    assert saved.steps[0]["params"] == {"target": cargo}
    search_tool = next(t for t in model.bound_tools if t["function"]["name"] == "go2_find_object")
    assert "target" not in search_tool["function"]["parameters"]["properties"]
    assert len(model.inputs) == 1


class Choice(BaseModel):
    choice: Literal["left", "right"]
    target: str | None
    speech: str = ""


@pytest.mark.asyncio
@pytest.mark.parametrize("speech", [
    # Offline model stubs for sentence/paragraph validation, never runtime dialogue.
    "第一句测试文本。第二句测试文本。",
    "第一句测试文本！第二句测试文本？",
    "第一段测试文本。\n第二段测试文本。",
])
async def test_game_final_preserves_multiple_sentences_in_original_graph(entry, monkeypatch, speech):
    from cloud.peers.games.role_graph import GameFinal

    model = ScriptedModel([AIMessage(content="", tool_calls=[{
        "name": DECISION_TOOL, "id": "game-final", "args": {"speech": speech}}])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "runtime_for", lambda _: None)
    monkeypatch.setattr(go2_graph, "go2", SimpleNamespace(is_connected=True))
    kwargs = dict(schema=GameFinal, instructions="自然回应本局最终结果。",
                  thread_id="multisentence-final", deadline=time.time() + 60)
    first = await decide("go2", "final", {}, **kwargs)
    second = await decide("go2", "final", {}, **kwargs)
    assert first["speech"] == second["speech"] == speech
    assert len(model.inputs) == 1


@pytest.mark.asyncio
async def test_game_choice_uses_original_go2_graph_native_tools_and_replays(entry, monkeypatch):
    model = ScriptedModel([AIMessage(content="", tool_calls=[{
        "name": DECISION_TOOL, "id": "stage-choice",
        "args": {"choice": "left", "target": None,
                 "decision_reason": "当前观察支持左侧目标。", "evidence_refs": ["private-scene"]}}])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "runtime_for", lambda _: None)
    monkeypatch.setattr(go2_graph, "go2", SimpleNamespace(is_connected=True))
    arguments = dict(schema=Choice, instructions="请从本阶段选项中选择。",
                     thread_id="original-device-choice", deadline=time.time() + 60)
    now = time.time()
    private_facts = {"stage": "choice", "space": {
        "activity": {"activity_id": "act_stage", "context": "本局目标", "started_at": now},
        "recent_discoveries": [{"event_id": "private-scene", "event_type": "observation",
            "activity_id": "act_stage", "unit_id": "go2", "ts": now, "summary": "左侧有蓝色目标"}]}}
    first = await decide("go2", "choice", private_facts, **arguments)
    second = await decide("go2", "choice", private_facts, **arguments)
    assert first == second == {"choice": "left", "target": None, "speech": ""}
    assert len(model.inputs) == 1
    assert [tool["function"]["name"] for tool in model.bound_tools] == [DECISION_TOOL]
    assert any("submit_game_decision" in str(message.content) for message in model.inputs[0])
    assert not any(kind in {"agent_reply", "reply_error", "agent_progress"} for kind, _ in entry)


@pytest.mark.asyncio
async def test_subtask_retries_in_original_graph_then_reports_trusted_completion(entry, monkeypatch):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "go2_observe", "id": "attempt1", "args": {}}]),
        AIMessage(content="", tool_calls=[{"name": "go2_observe", "id": "attempt2", "args": {}}]),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "runtime_for", lambda _: None)
    monkeypatch.setattr(go2_graph, "go2", SimpleNamespace(
        is_connected=True, state_snapshot=lambda: {}, body_state="standing"))
    execute = AsyncMock(side_effect=[
        {"result": "error", "error": "frame_unavailable"},
        {"result": "ok", "message": "已核对目标。", "found": True},
    ])
    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    def completion(_name, _params, result):
        return {**result, "final": result.get("found") is True}
    context = ExecutionContext("peer_game_retry", "visitor", False,
                               origin_kind="agent_autonomous", expires_at=int(time.time() + 60))
    result = await run_device_task("go2", "核对指定目标。", context=context,
        run_id="retry-in-original-brain", deadline=time.time() + 50,
        allowed_tools={"go2_observe"}, result_transform=completion,
        share_result=False, reply_required=False)
    assert result["result"] == "ok"
    assert [step["result"]["result"] for step in result["tool_results"]] == ["error", "ok"]
    assert len(model.inputs) == execute.await_count == 2
    assert all(facts.get("context_scope") == "process" for kind, facts in entry
               if kind == "agent_progress")
    assert not any(kind in {"agent_reply", "reply_error"} for kind, _ in entry)


def test_business_completion_cannot_override_an_unknown_physical_outcome():
    task = RoleTask(result_transform=lambda _n, _p, receipt: {**receipt, "final": True})
    success = task.transform("push", {}, {"result": "ok"})
    run = SimpleNamespace(status="unknown", steps=[
        {"result": {"result": "unknown"}}, {"result": success}])
    task.settle_status(run)
    assert run.status == "unknown"


@pytest.mark.asyncio
async def test_lamp_stage_enters_original_service_and_graph_without_extra_reply(facts, monkeypatch, tmp_path):
    from cloud.agents.lamp import graph as lamp_graph
    from cloud.agents.lamp import prompts
    from cloud.esp32.service import ESP32Agent
    from types import MethodType
    agent, _physical, _space = facts
    for name in ("SSM_CHECKPOINT_DIR", "SSM_EFFECT_LEDGER_DIR", "SSM_RUN_INDEX_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    model = ScriptedModel([AIMessage(content="", tool_calls=[{
        "name": DECISION_TOOL, "id": "lamp-choice",
        "args": {"choice": "right", "target": "蓝点", "speech": ""}}])])
    agent._llm = model
    agent._conversation_lock = threading.Lock()
    agent._conversations = 0
    agent.run_user_loop = MethodType(ESP32Agent.run_user_loop, agent)
    monkeypatch.setattr("cloud.esp32.service.get_agent", lambda: agent)
    monkeypatch.setattr(lamp_graph, "get_profile", prompts.get_profile)
    monkeypatch.setattr(lamp_graph, "visible_schemas", lambda **_kw: {})
    monkeypatch.setattr(lamp_graph.cards, "get_registry",
                        lambda: SimpleNamespace(get_card=lambda _uid: None))
    publish = []
    monkeypatch.setattr("cloud.api.intents.publish_agent_event",
                        lambda kind, **fields: publish.append((kind, fields)) or {})
    decision = await decide("lamp", "choice", {"stage": "choice"}, schema=Choice,
                            instructions="选择本阶段的目标。", thread_id="lamp-original-brain",
                            deadline=time.time() + 60)
    assert decision["choice"] == "right"
    assert len(model.inputs) == 1 and agent._conversations == 0
    assert not any(kind in {"agent_reply", "reply_error", "agent_progress"} for kind, _ in publish)


@pytest.mark.asyncio
async def test_closing_only_allows_final_submission_and_reuses_existing_line(monkeypatch, tmp_path):
    from cloud.space import activity
    from cloud.peers.games import role_graph
    from cloud.peers.games.scope import (
        game_scope_available, terminal_game_scope, terminal_scope_available,
    )
    store = activity.ActivityStore(tmp_path / "activity.json")
    store._current = {"activity_id": "act_final", "status": "active"}
    store._events["act_final"] = [{"event_type": "game_closing", "game_instance_id": "game_final"}]
    monkeypatch.setattr(activity, "get_activity_store", lambda: store)
    with terminal_game_scope("act_final", "game_final", "push_delivery"):
        assert terminal_scope_available() and not game_scope_available()
        assert not RoleTask(allowed_tools=frozenset({"go2_move"})).available()
        assert RoleTask(terminal=True, decision_schema=Choice,
                        allowed_tools=frozenset({DECISION_TOOL})).available()
    seen = []
    async def final_decision(role, phase, _facts, **kwargs):
        seen.append((role, phase, kwargs["terminal"]))
        assert terminal_scope_available() and not game_scope_available()
        return {"speech": "这局结束，我先歇会儿。", "emotion": "neutral"}
    monkeypatch.setattr(role_graph, "decide", final_decision)
    context = ExecutionContext("act_final", "visitor", False,
                               origin_kind="agent_autonomous", expires_at=int(time.time() + 60))
    replies = await role_graph.final_responses(SimpleNamespace(game_id="push_delivery", metadata=lambda: {}),
        activity_id="act_final", invitation_id="game_final", context=context,
        result={"result": "ok", "role_replies": {"go2": {
            "terminal": True, "result": "ok", "speech": "我朝指定方向尝试推送了。\n已经停稳，这局结束啦。"}}},
        deadline=time.time() + 30)
    assert seen == [("lamp", "final", True)]
    assert len(replies) == 2 and replies[1]["speech"] == "我朝指定方向尝试推送了。\n已经停稳，这局结束啦。"
    store._events["act_final"].append({"event_type": "game_ended", "game_instance_id": "game_final"})
    with terminal_game_scope("act_final", "game_final", "push_delivery"):
        assert not terminal_scope_available()
