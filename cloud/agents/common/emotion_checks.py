"""Avatar metadata is optional and cannot change execution or delivery semantics."""

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.emotions import EMOTIONS, normalize_emotion, parse_emotional_reply
from cloud.agents.common.replies import finalize_reply
from cloud.agents.common.runner import DialogueResponse, GraphRun, run_role_graph


@pytest.mark.parametrize("emotion", [None, "excited", 3, {}, [], True])
def test_bad_emotion_keeps_valid_dialogue_and_final_reply(emotion):
    payload = {"kind": "final", "text": "动作已确认。", "emotion": emotion}
    assert DialogueResponse.model_validate(payload).emotion == "neutral"
    assert parse_emotional_reply(json.dumps(payload)) == ("动作已确认。", "neutral")


def test_legacy_prose_and_missing_emotion_remain_valid():
    assert parse_emotional_reply("今天想聊什么？") == ("今天想聊什么？", "neutral")
    assert DialogueResponse(kind="question", text="哪一个？").emotion == "neutral"
    assert GraphRun("r", "s", "hello", time.time()).emotion == "neutral"


def test_business_reply_fields_remain_strict():
    with pytest.raises(ValidationError):
        DialogueResponse(kind="execute", text="继续", emotion="happy")
    with pytest.raises(ValidationError):
        DialogueResponse(kind="final", text="正常", emotion="happy", actor="admin")
    with pytest.raises(Exception):
        parse_emotional_reply('{"emotion":"happy"}')


@pytest.mark.parametrize("emotion", EMOTIONS)
@pytest.mark.asyncio
async def test_model_emotion_uses_same_reply_call_without_actions(emotion, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(
        {"kind": "final", "text": "我在听。", "emotion": emotion}))))
    run = GraphRun("emotion", "s", "你好", time.time() + 30, agent_id="go2")
    await run_role_graph(run, model=model, tool_schemas={},
        build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=lambda *_a: pytest.fail("emotion must never dispatch a tool"),
        finalize=lambda current: finalize_reply(current, model=model, instructions="回应用户。"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert run.response == "我在听。" and run.emotion == emotion
    assert run.status == "ok" and run.steps == []
    model.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("status", ["ok", "unknown", "cancelled", "error"])
@pytest.mark.asyncio
async def test_receipt_reply_emotion_never_changes_status_or_receipt(status, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)
    receipt = {"result": status, "task_id": "physical-1"}
    run = GraphRun("reply", "s", "执行动作", time.time() + 30, agent_id="go2",
        status=status, steps=[{"tool": "go2_hello", "step_id": "one", "result": receipt}])
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(
        {"text": "我看到了这次结果。", "emotion": "curious"}))))
    await finalize_reply(run, model=model, instructions="只根据真实结果回复。")
    assert run.emotion == "curious" and run.response == "我看到了这次结果。"
    assert run.status == status and run.steps[0]["result"] == receipt
    model.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("emotion", ["happy", {"wrong": "type"}])
@pytest.mark.asyncio
async def test_action_then_emotional_closing_keeps_final_and_exactly_one_dispatch(emotion, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)

    class Action(BaseModel):
        model_config = ConfigDict(extra="forbid")
        brightness: int

    class Model:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools, **_kwargs):
            assert set(tools[0]["function"]["parameters"]["properties"]) == {"brightness", "final"}
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="", tool_calls=[{
                    "name": "set_light", "id": "physical-step", "args": {"brightness": 100}}])
            assert self.calls == 2, "no planning or emotion-classification call after final"
            return AIMessage(content=json.dumps({"text": "调好了。", "emotion": emotion}))

    dispatched = []
    receipt = {"result": "accepted", "task_id": "confirmed-task", "brightness": 100}

    def execute(name, args, step_id):
        dispatched.append((name, args, step_id))
        return receipt

    model = Model()
    run = GraphRun("action-emotion", "s", "调灯", time.time() + 30, agent_id="esp32_desk_led")
    await run_role_graph(run, model=model, tool_schemas={"set_light": Action},
        build_messages=lambda current: [HumanMessage(content=current.goal)], execute=execute,
        finalize=lambda current: finalize_reply(current, model=model, instructions="回应真实结果。"),
        max_effects=1, checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert dispatched == [("set_light", {"brightness": 100}, "physical-step")]
    assert model.calls == 2 and len(run.steps) == 1 and run.status == "ok"
    assert run.steps[0]["result"] == {"result": "ok", "device_result": "accepted",
        "final": True, "task_id": "confirmed-task", "brightness": 100}
    assert receipt == {"result": "accepted", "task_id": "confirmed-task", "brightness": 100}
    assert run.emotion == normalize_emotion(emotion) and run.response == "调好了。"


@pytest.mark.asyncio
async def test_invalid_reply_fallback_clears_previous_emotion(monkeypatch):
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)
    run = GraphRun("fallback", "s", "执行动作", time.time() + 30, agent_id="go2",
        status="unknown", emotion="happy", decision_draft='{"text": null}')
    model = SimpleNamespace(ainvoke=AsyncMock())
    await finalize_reply(run, model=model, instructions="回应用户。")
    assert run.emotion == "neutral" and run.status == "unknown"
    assert run.reply_source == "runtime"
    model.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_physical_reply_carries_emotion_and_recovery_does_not_recompose(tmp_path, monkeypatch):
    from cloud.agents.lamp.workflows import physical_reply

    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr(physical_reply, "get_profile", lambda: {})
    monkeypatch.setattr(physical_reply, "persona_reply_prompt", lambda *_a: "回应实际回执。")
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(
        content='{"text":"灯调好了。","emotion":"relaxed"}')))
    events = []

    def publish(kind, **facts):
        events.append((kind, facts))
        return {"event_id": "reply-event"}

    options = dict(task_id="physical", goal="开灯", action="set_led_state", params={"state": "ON"},
        trace_id="trace", parent_run_id="parent", unit_id="esp32_desk_led", session_id="s",
        agent=SimpleNamespace(_llm=model),
        device_state=SimpleNamespace(get_task_result=lambda _id: {"result": "accepted"}),
        registry=SimpleNamespace(get_card=lambda _id: {"online": True}), publish=publish,
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"))
    first = await physical_reply.run_physical_reply(**options)
    restored = await physical_reply.run_physical_reply(**options)
    assert first == restored
    assert first["emotion"] == "relaxed" and first["result"] == "accepted"
    assert first["response"] == "灯调好了。"
    assert len(events) == 1 and events[0][1]["emotion"] == "relaxed"
    model.ainvoke.assert_awaited_once()


def test_mqtt_feedback_preserves_emotion_and_service_identity(monkeypatch):
    from cloud.api import intents
    from cloud.orchestrator import tools

    published, forwarded = [], []
    monkeypatch.setattr(tools, "_mqtt", SimpleNamespace(
        publish=lambda topic, data, **_k: published.append((topic, data))))
    monkeypatch.setattr(intents, "publish_agent_event", lambda kind, **facts: forwarded.append((kind, facts)))
    tools.do_publish_ui_event("private", "agent_reply", run_id="r", trace_id="r",
                              status="ok", text="完成。", emotion="happy")
    assert intents.handle_mqtt_message(*published[0])
    assert forwarded[0][1]["emotion"] == "happy"
    assert forwarded[0][1]["agent_id"] == "cloud_orchestrator"
    assert forwarded[0][1]["scope"] == "session"
    assert forwarded[0][1]["session_id"] == "private"
    forged = json.loads(published[0][1])
    forged["agent_id"] = "go2"
    intents.handle_mqtt_message(published[0][0], json.dumps(forged))
    assert len(forwarded) == 1


@pytest.mark.parametrize("emotion", ["sad", None, {"status": "ok"}])
def test_api_publishes_only_normalized_emotion_without_changing_result(emotion, monkeypatch):
    from cloud.api import intents

    delivered = []
    monkeypatch.setattr(intents, "_events", SimpleNamespace(
        emit=lambda session, event: delivered.append((session, event))))
    event = intents.publish_agent_event("agent_reply", agent_id="go2", status="unknown",
        session_id="private", run_id="r", text="还没有确认。", emotion=emotion)
    assert event["emotion"] == normalize_emotion(emotion)
    assert event["status"] == "unknown" and event["scope"] == "session"
    assert delivered == [("private", event)]
    with pytest.raises(ValueError, match="invalid_agent_id"):
        intents.publish_agent_event("agent_reply", agent_id="unknown", status="ok",
            session_id="private", text="完成。", emotion="happy")
