"""Native human-input interrupts preserve receipts across role graph recovery."""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.messages import role_messages
from cloud.agents.common.runner import GraphRun, run_role_graph


@tool
def adjust_device() -> str:
    """A device action whose receipt must survive a human-input pause."""
    raise AssertionError("declaration only")


class Model(JsonReplyModel):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []
        self.tools = []

    def bind_tools(self, tools, **kwargs):
        assert kwargs == {"parallel_tool_calls": False}
        self.tools = tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return next(self.replies)


def response(kind, text):
    return AIMessage(content=json.dumps({"kind": kind, "text": text}, ensure_ascii=False))


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["cloud_orchestrator", "esp32_desk_led", "go2"])
async def test_repeated_clarifications_survive_reopen_and_next_turn(agent_id, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    executed = []

    async def execute(name, args, step_id):
        executed.append(step_id)
        return {"result": "ok", "receipt": "confirmed"}

    goal = "先开灯，再调整阅读光"
    deadline = time.time() + 60
    common = dict(tool_schemas={"adjust_device": adjust_device}, execute=execute,
                  build_messages=lambda run: role_messages("当前角色", run.goal,
                      context={"sensor": "fresh"}, supplement=run.supplement))

    def new_run():
        return GraphRun("clarifications", "session", goal, deadline, agent_id=agent_id)

    def saver():
        return FileCheckpointSaver(tmp_path / "checkpoints")

    ledger = FileEffectLedger(tmp_path / "effects")
    first = Model([
        AIMessage(content="", tool_calls=[{
            "name": "adjust_device", "args": {"final": False}, "id": "physical-on"}]),
        response("question", "偏暖还是偏冷？")])
    paused = await run_role_graph(new_run(), model=first, checkpointer=saver(),
                                  effect_ledger=ledger, **common)
    assert paused.status == "needs_clarification"
    config = {"configurable": {"thread_id": thread_id_for(agent_id, "session")}}

    def saved_dialogue():
        return saver().get_tuple(config).checkpoint["channel_values"]["messages"]

    assert [m.content for m in saved_dialogue()] == [goal, "偏暖还是偏冷？"]
    second = Model([response("question", "还要再亮一点吗？")])
    await run_role_graph(new_run(), model=second, checkpointer=saver(),
                         effect_ledger=ledger, resume=True, resume_value="暖一点吧", **common)
    assert [m.content for m in saved_dialogue()] == [
        goal, "偏暖还是偏冷？", "暖一点吧", "还要再亮一点吗？"]
    third = Model([response("final", "明白，保留暖光偏好。")])
    final = await run_role_graph(new_run(), model=third, checkpointer=saver(),
        effect_ledger=ledger, resume=True, resume_value="不用再亮了", **common)
    assert final.status == "ok"
    expected = [goal, "偏暖还是偏冷？", "暖一点吧", "还要再亮一点吗？", "不用再亮了"]
    dialogue = [m for m in third.inputs[0] if isinstance(m, (HumanMessage, AIMessage))
                and m.name is None and not getattr(m, "tool_calls", None)]
    assert [m.content for m in dialogue] == expected
    assert [m.type for m in dialogue] == ["human", "ai", "human", "ai", "human"]
    assert final.steps[0]["result"]["receipt"] == "confirmed"
    assert executed == ["physical-on"]
    assert [m.content for m in saved_dialogue()] == [*expected, "明白，保留暖光偏好。"]

    following = Model([response("final", "你说过暖一点。")])
    await run_role_graph(GraphRun("next-turn", "session", "我刚才选的什么光？", deadline,
        agent_id=agent_id), model=following, checkpointer=saver(), effect_ledger=ledger, **common)
    contents = [m.content for m in following.inputs[0]]
    assert contents.count("暖一点吧") == contents.count(goal) == 1
    assert "不用再亮了" in contents
    assert len(saved_dialogue()) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_id", ["cloud_orchestrator", "esp32_desk_led", "go2"])
async def test_question_interrupt_reopens_without_repeating_completed_action(
        agent_id, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event",
                        lambda kind, **fields: events.append((kind, fields)))
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    ledger = FileEffectLedger(tmp_path / "effects")
    current = GraphRun("human-input", "session", "先调整，再去指定位置", time.time() + 60,
                       agent_id=agent_id)
    executed = []

    async def execute(name, args, step_id):
        executed.append((name, step_id))
        return {"result": "ok", "receipt": "completed"}

    model = Model([
        AIMessage(content="", tool_calls=[{
            "name": "adjust_device", "args": {"final": False}, "id": "physical-1"}]),
        response("question", "接下来去哪里？"),
    ])
    common = dict(tool_schemas={"adjust_device": adjust_device}, execute=execute,
                  build_messages=lambda run: [HumanMessage(content=run.supplement or run.goal)])
    paused = await run_role_graph(current, model=model, checkpointer=saver,
                                  effect_ledger=ledger, **common)
    assert paused.status == "needs_clarification"
    assert paused.question == "接下来去哪里？"
    assert len(paused.steps) == 1
    assert {item["function"]["name"] for item in model.tools} == {"adjust_device"}

    reopened = FileCheckpointSaver(saver.root)
    snapshot = reopened.get_tuple({"configurable": {
        "thread_id": thread_id_for(agent_id, "session")}})
    assert any(channel == "__interrupt__" for _, channel, _ in snapshot.pending_writes)
    assert snapshot.checkpoint["channel_values"]["run"]["steps"][0]["result"]["receipt"] == "completed"
    resumed_model = Model([response("final", "明白了。")])
    fresh_run = GraphRun("human-input", "session", current.goal, current.deadline, agent_id=agent_id)
    resumed = await run_role_graph(
        fresh_run, model=resumed_model, checkpointer=reopened,
        effect_ledger=FileEffectLedger(ledger.root), resume=True, resume_value="客厅", **common)
    assert resumed.status == "ok"
    assert resumed.question == ""
    assert resumed.steps[0]["result"]["receipt"] == "completed"
    assert executed == [("adjust_device", "physical-1")]
    assert any(isinstance(message, HumanMessage) and message.content == "客厅"
               for message in resumed_model.inputs[0])
    assert any(kind == "run.paused" for kind, _ in events)


@pytest.mark.asyncio
async def test_final_response_with_question_mark_does_not_pause(tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    current = GraphRun("final-question-mark", "session", "解释这个问题", time.time() + 30,
                       agent_id="go2")
    final = await run_role_graph(
        current, model=Model([response("final", "你问的是‘灯亮了吗？’，目前灯已亮。")]),
        tool_schemas={}, build_messages=lambda run: [HumanMessage(content=run.goal)],
        execute=lambda *_a: pytest.fail("unexpected action"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert final.status == "ok"
    assert final.question == ""
    assert "？" in final.decision_draft


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    '{"kind":"question"}',
    '{"kind":"unknown","text":"去哪里？"}',
    '{"kind":"question","text":"   "}',
    '{"kind":"question","text":"去哪里？","tool":"adjust_device"}',
    '{broken json',
])
async def test_invalid_dialogue_response_cannot_pause_or_execute(content, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    current = GraphRun("invalid-dialogue", "session", "向前移动", time.time() + 30, agent_id="go2")
    final = await run_role_graph(
        current, model=Model([AIMessage(content=content)]),
        tool_schemas={"adjust_device": adjust_device},
        build_messages=lambda run: [HumanMessage(content=run.goal)],
        execute=lambda *_a: pytest.fail("invalid reply dispatched an action"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert final.status == "error"
    assert final.question == ""
    assert final.steps == []


def test_real_chat_client_preserves_json_output_and_non_strict_tools():
    import httpx
    from langchain_openai import ChatOpenAI

    from cloud.agents.common.runner import DialogueResponse

    requests = []
    replies = iter([
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "physical-sdk", "type": "function",
            "function": {"name": "adjust_device", "arguments": "{}"}}]},
        {"role": "assistant", "content": json.dumps(
            {"kind": "question", "text": "接下来去哪里？"}, ensure_ascii=False)},
        {"role": "assistant", "content": json.dumps(
            {"kind": "final", "text": "明白了。"}, ensure_ascii=False)},
    ])

    def respond(request):
        requests.append(json.loads(request.content))
        message = next(replies)
        return httpx.Response(200, json={
            "id": f"completion-{len(requests)}", "object": "chat.completion",
            "created": 0, "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(model="deepseek-v4-flash", api_key="test-only",
                           base_url="https://ark.invalid/v3",
                           http_client=client, max_retries=0)
        bound = model.bind_tools([adjust_device], parallel_tool_calls=False).bind(
            extra_body={"response_format": {"type": "json_object"}})
        tool_call = bound.invoke([HumanMessage(content="先调整再去那里")])
        question = bound.invoke([HumanMessage(content="调整完成，继续规划")])
        final = bound.invoke([HumanMessage(content="客厅")])

    assert tool_call.tool_calls[0]["name"] == "adjust_device"
    assert DialogueResponse.model_validate_json(question.content).kind == "question"
    assert DialogueResponse.model_validate_json(final.content).kind == "final"
    assert len(requests) == 3
    for payload in requests:
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["parallel_tool_calls"] is False
        assert payload["tools"][0]["function"]["name"] == "adjust_device"
        assert payload["tools"][0]["function"].get("strict") is not True
