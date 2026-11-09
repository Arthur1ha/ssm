"""Model-free checks of role boundaries, real receipts and final call counts."""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.messages import reply_messages, role_messages
from cloud.agents.common.progress import progress_messages
from cloud.agents.common.replies import factual_fallback, finalize_reply, valid_persona_reply
from cloud.agents.common.runner import GraphRun, run_role_graph


def run(**fields):
    return GraphRun("messages", "session", "准备看一会儿书", time.time() + 30,
                    agent_id="esp32_desk_led", **fields)


@pytest.mark.parametrize("role", ["lamp", "steward", "go2"])
def test_role_decisions_separate_instructions_request_and_live_facts(role, monkeypatch):
    from cloud.agents.lamp import prompts as lamp
    from cloud.agents.steward import prompts as steward
    from cloud.agents.go2 import prompts as robot

    profile = {"agent_id": "test", "name": "测试角色", "prompt": "保持当前人格",
               "revision": 1, "traits": {"social_initiative": 50,
                   "adjustment_sensitivity": 50, "clarification_tendency": 50,
                   "curiosity": 50, "extraversion": 50, "boldness": 50, "warmth": 50}}
    for module in (lamp, steward, robot):
        monkeypatch.setattr(module, "get_profile", lambda: profile)
        monkeypatch.setattr(module, "agent_names", lambda: {"test": "测试角色"})
    space = {"peers": [], "activity": None, "recent_discoveries": []}
    monkeypatch.setattr(lamp, "get_space_context", lambda *a, **k: space)
    monkeypatch.setattr(steward, "get_space_context", lambda *a, **k: space)
    monkeypatch.setattr(steward, "emit_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(robot, "prompt_space_context", lambda: space)
    monkeypatch.setattr(robot.go2, "state_snapshot", lambda: {"body_state": "standing"})
    current = run()
    if role == "lamp":
        agent = SimpleNamespace(_state=SimpleNamespace(
            actuator_snapshot=lambda: {"lamp": {"state": {"r": 255, "brightness": 200}}}))
        messages = lamp.decision_messages(current, agent=agent, unit_id="lamp", supplement="就用上次那档")
    elif role == "steward":
        messages = steward.build_messages(current, "就用上次那档")
    else:
        messages = robot.build_messages(current, supplement="就用上次那档", memory="阅读配置")
    assert isinstance(messages[0], SystemMessage)
    assert current.goal not in messages[0].content
    assert "就用上次那档" not in messages[0].content
    assert "人格用于表达" in messages[0].content
    assert "已有" in messages[0].content
    assert [message.content for message in messages if isinstance(message, HumanMessage)
            and message.name is None] == [current.goal, "就用上次那档"]
    contexts = [message for message in messages if message.name == "context"]
    assert len(contexts) == 1
    assert "上下文数据" in contexts[0].content
    assert not any("recent_dialogue" in message.content for message in messages)


def test_reply_and_progress_keep_actual_tool_call_and_receipt_roles():
    current = run(steps=[{"step_id": "set-1", "tool": "set_led_color",
                         "params": {"r": 255, "g": 236, "b": 210},
                         "result": {"result": "ok", "task_id": "device-1"}}])
    for messages in (reply_messages("用自己的口吻回复", current),
                     progress_messages("当前人格", current.goal, current.steps[0])):
        assert isinstance(messages[0], SystemMessage)
        call, receipt = messages[-2:]
        assert isinstance(call, AIMessage) and isinstance(receipt, ToolMessage)
        assert call.tool_calls[0]["id"] == receipt.tool_call_id == "set-1"
        assert json.loads(receipt.content) == current.steps[0]["result"]
        assert current.goal in [message.content for message in messages]


@pytest.mark.parametrize("agent_id", ["esp32_desk_led", "go2", "cloud_orchestrator"])
@pytest.mark.asyncio
async def test_final_dialogue_is_used_without_a_second_model_call(agent_id, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *a, **k: None)
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *a, **k: None)

    class Model(JsonReplyModel):
        def __init__(self): self.calls = 0
        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(content="记得，我们刚才在调阅读时用的暖白。")

    model = Model()
    current = run()
    current.agent_id = agent_id
    await run_role_graph(current, model=model, tool_schemas={},
        build_messages=lambda value: role_messages("以当前人格回应", value.goal),
        execute=lambda *a: pytest.fail("unexpected action"),
        finalize=lambda value: finalize_reply(value, model=model, instructions="直接回复"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert model.calls == 1
    assert current.response == "记得，我们刚才在调阅读时用的暖白。"
    assert current.reply_source == "decision_model"


@pytest.mark.asyncio
async def test_invalid_final_uses_facts_without_retrying_persona_model(monkeypatch):
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *a, **k: None)
    current = run(status="ok", decision_draft="记住了，灯已经调好了。",
                  steps=[{"tool": "propose_training", "effectful": True,
                          "result": {"result": "ok", "result_kind": "training_proposal",
                                     "proposal_id": "pending"}}])
    model = SimpleNamespace(ainvoke=lambda *_a: pytest.fail("unexpected rewrite"))
    await finalize_reply(current, model=model, instructions="以自己的身份回复")
    assert current.reply_source == "runtime"
    assert "核对" in current.response
    assert "调好了" not in current.response and "记住了" not in current.response


@pytest.mark.asyncio
async def test_direct_receipt_composes_once_with_original_request_and_tool_message(monkeypatch):
    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *a, **k: None)
    inputs = []

    async def invoke(messages):
        inputs.append(messages)
        return AIMessage(content="这档暖白调好了。")

    current = run(status="ok", steps=[{"tool": "SET_COLOR", "step_id": "device-2",
        "result": {"result": "ok"}, "params": {"r": 255, "g": 236, "b": 210}}])
    await finalize_reply(current, model=SimpleNamespace(ainvoke=invoke), instructions="灯的人格")
    assert current.response == "这档暖白调好了。" and len(inputs) == 1
    assert isinstance(inputs[0][0], SystemMessage)
    assert any(message.content == current.goal for message in inputs[0])
    assert isinstance(inputs[0][-1], ToolMessage)


def test_query_and_proposal_success_never_imply_physical_completion():
    for tool in ("list_training", "propose_training"):
        current = run(status="ok", steps=[{"tool": tool, "effectful": tool == "propose_training",
                                           "result": {"result": "ok"}}])
        answer = factual_fallback(current.agent_id, current.status, current)
        assert "灯已经" not in answer and "调好了" not in answer
    assert not valid_persona_reply("我已经保存好了。", current)


@pytest.mark.asyncio
async def test_expired_direct_receipt_does_not_start_another_model_call():
    current = run(status="timeout", steps=[{"tool": "SET_COLOR", "result": {"result": "timeout"}}])
    current.deadline = time.time() - 1
    await finalize_reply(current, model=None, instructions="直接回复")
    assert current.reply_source == "runtime" and "确认" in current.response


@pytest.mark.asyncio
async def test_empty_response_retry_is_runtime_instruction_not_user_wording(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *a, **k: None)
    inputs = []

    class Model(JsonReplyModel):
        async def ainvoke(self, messages):
            inputs.append(messages)
            return AIMessage(content="" if len(inputs) == 1 else "我在，继续说。")

    current = run()
    await run_role_graph(current, model=Model(), tool_schemas={},
        build_messages=lambda value: role_messages("当前角色", value.goal),
        execute=lambda *a: pytest.fail("unexpected action"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert len(inputs) == 2 and isinstance(inputs[1][-1], SystemMessage)
    assert [message.content for message in inputs[1] if isinstance(message, HumanMessage)] == [current.goal]


@pytest.mark.asyncio
async def test_physical_reply_keeps_receipt_wait_and_publish_idempotency(tmp_path, monkeypatch):
    from cloud.agents.lamp.workflows import physical_reply

    monkeypatch.setattr("cloud.agents.common.replies.emit_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(physical_reply, "get_profile", lambda: {
        "prompt": "灯的人格", "name": "测试灯", "traits": {}})
    inputs, published = [], []

    async def invoke(messages):
        inputs.append(messages)
        return AIMessage(content="这档暖白调好了。")

    def publish(kind, **fields):
        published.append((kind, fields))
        return {"event_id": "physical-final"}

    kwargs = dict(task_id="physical-task", goal="用这档暖白", action="SET_COLOR",
        params={"r": 255, "g": 236, "b": 210}, trace_id="trace", parent_run_id="parent",
        unit_id="esp32_desk_led", session_id="session",
        agent=SimpleNamespace(_llm=SimpleNamespace(ainvoke=invoke)),
        device_state=SimpleNamespace(get_task_result=lambda _id: {"result": "ok"}),
        registry=SimpleNamespace(get_card=lambda _id: {"online": True}), publish=publish,
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"))
    first = await physical_reply.run_physical_reply(**kwargs)
    second = await physical_reply.run_physical_reply(**kwargs)
    assert first == second and first["response"] == "这档暖白调好了。"
    assert len(inputs) == 1 and len(published) == 1
    assert isinstance(inputs[0][0], SystemMessage) and isinstance(inputs[0][-1], ToolMessage)


@pytest.mark.parametrize("step,answer", [
    ({"tool": "propose_training", "effectful": True, "result": {"result": "ok"}}, "我已经保存好了。"),
    ({"tool": "SET_COLOR", "effectful": True, "result": {"result": "accepted"}}, "灯调好了。"),
])
def test_progress_narration_cannot_promote_draft_or_acceptance_to_completion(step, answer, monkeypatch):
    from cloud.agents.common import progress

    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(progress, "_slots", slots)
    published = []
    progress.submit_progress("当前人格", "调整阅读配置", step, lambda _messages: answer,
                             published.append, threading.Event(), time.time() + 5)
    assert slots.acquire(timeout=2), "narration worker did not release its slot"
    assert published == []
