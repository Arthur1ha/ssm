"""Go2's prospective Graph entry preserves result and event contracts in isolation."""

import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.go2 import graph as go2_graph
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.run_index import FileRunIndex
from cloud.agents.go2.run_view import Go2RunView, stable_reply_event_id
from cloud.go2.execution import ExecutionContext


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def bind_tools(self, tools, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return next(self.replies)


@pytest.fixture
def entry(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setattr(go2_graph, "get_store", lambda: Go2RunView(
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        index=FileRunIndex(tmp_path / "index")))
    monkeypatch.setattr(go2_graph, "go2_messages", lambda run, **kwargs: [
        HumanMessage(content=str(run.steps))])
    monkeypatch.setattr("cloud.agents.go2.personality.profile.get_profile",
                        lambda: {"prompt": "Go2", "revision": 1, "traits": {}})
    monkeypatch.setattr("cloud.agents.go2.personality.behavior.reply_persona",
                        lambda _profile: "Go2 persona")
    monkeypatch.setattr(go2_graph, "persona_reply_prompt", lambda *_a, **_k: "reply")
    monkeypatch.setattr(go2_graph, "valid_persona_reply", lambda *_a, **_k: True)
    monkeypatch.setattr("cloud.agents.common.progress.submit_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(go2_graph, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    published = []

    def publish(event_type, **fields):
        published.append((event_type, fields))
        return {"event_id": fields.get("event_id") or "evt-test"}

    monkeypatch.setattr("cloud.api.intents.publish_agent_event", publish)
    return published


@pytest.mark.asyncio
async def test_verified_motion_result_and_reply_shape(entry, monkeypatch, tmp_path):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "go2_sport",
                                              "args": {"cmd": "Hello"}, "id": "tool-1"}]),
        AIMessage(content="动作结束"),
        AIMessage(content="汪！招呼打完啦。"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph.go2, "is_connected", True)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append((name, params, context.actor))
        return {"result": "ok", "receipt": "webrtc"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("go2-session", "admin", True, expires_at=int(time.time() + 60))
    streamed_steps = []
    result = await go2_graph.run_go2_loop("go2-session", "和我打招呼", context=context,
                                          run_id="go2-test-verified", on_step=streamed_steps.append)
    assert result["result"] == "ok"
    assert result["response"] == "汪！招呼打完啦。"
    assert result["actions_taken"] == ["go2_sport"]
    assert result["tool_results"][0]["result"]["receipt"] == "webrtc"
    assert dispatched == [("go2_sport", {"cmd": "Hello"}, "admin")]
    assert result["event_id"] == stable_reply_event_id(
        "go2-test-verified", session_id="go2-session")
    assert streamed_steps[0]["step_id"] == "tool-1"
    assert streamed_steps[0]["result"]["result"] == "ok"
    assert any(kind == "agent_progress" and fields.get("stage") == "observed"
               and fields["step_id"] == "tool-1" and fields["source"] == "device_chat"
               for kind, fields in entry)
    assert any(kind == "agent_reply" and fields["agent_id"] == "go2"
               and fields["reply_kind"] == "final" for kind, fields in entry)
    reopened = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                          index=FileRunIndex(tmp_path / "index"))
    stored = reopened.get("go2-test-verified", "go2-session")
    assert stored is not None
    assert stored.response == result["response"]
    assert stored.reply_event_id == result["event_id"]
    repeated = await go2_graph.run_go2_loop("go2-session", "和我打招呼", context=context,
                                             run_id="go2-test-verified")
    assert repeated == result
    assert dispatched == [("go2_sport", {"cmd": "Hello"}, "admin")]


@pytest.mark.asyncio
async def test_unconfirmed_motion_stops_following_tool(entry, monkeypatch):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "go2_sport",
                                              "args": {"cmd": "Hello"}, "id": "tool-2"}]),
        AIMessage(content="不应继续"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "accepted"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("go2-session-2", "admin", True, expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop("go2-session-2", "和我打招呼，然后跳舞", context=context,
                                          run_id="go2-test-accepted")
    assert result["result"] == "accepted"
    assert dispatched == ["go2_sport"]
    assert len(model.inputs) == 1
    assert result["actions_taken"] == []


@pytest.mark.asyncio
async def test_visitor_cannot_dispatch_admin_tool_from_model(entry, monkeypatch):
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "go2_list_people", "args": {}, "id": "tool-forged"},
    ])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "needs_persona_reply", lambda _run: False)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "ok"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("visitor-session", "visitor", False,
                               expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop("visitor-session", "列出人员", context=context,
                                          run_id="go2-test-visitor")
    assert result["result"] == "rejected"
    assert dispatched == []
    names = {tool["function"]["name"] for tool in model.bound_tools}
    assert "go2_list_people" not in names
    assert "ask_user" in names


@pytest.mark.asyncio
async def test_two_motion_calls_in_one_model_turn_execute_neither(entry, monkeypatch):
    model = ScriptedModel([AIMessage(content="", tool_calls=[
        {"name": "go2_sport", "args": {"cmd": "Hello"}, "id": "one"},
        {"name": "go2_sport", "args": {"cmd": "Dance1"}, "id": "two"},
    ])])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph, "needs_persona_reply", lambda _run: False)
    dispatched = []

    async def execute(name, params, *, context):
        dispatched.append(name)
        return {"result": "ok"}

    monkeypatch.setattr(go2_graph, "execute_tool", execute)
    context = ExecutionContext("parallel-session", "admin", True,
                               expires_at=int(time.time() + 60))
    result = await go2_graph.run_go2_loop("parallel-session", "打招呼并跳舞", context=context,
                                          run_id="go2-test-parallel")
    assert result["result"] == "blocked"
    assert result["actions_taken"] == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_clarification_resumes_from_reopened_checkpoint(entry, monkeypatch, tmp_path):
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "ask_user",
                                              "args": {"question": "去哪里？"}, "id": "question-1"}]),
        AIMessage(content="明白了，去客厅"),
        AIMessage(content="汪，去客厅呀！"),
    ])
    monkeypatch.setattr(go2_graph, "get_text_llm", lambda: model)
    monkeypatch.setattr(go2_graph.go2, "is_connected", True)
    context = ExecutionContext("clarify-session", "admin", True,
                               expires_at=int(time.time() + 60))
    first = await go2_graph.run_go2_loop("clarify-session", "去那边", context=context,
                                         run_id="go2-test-clarify")
    assert first["result"] == "needs_clarification"
    assert first["question"] == "去哪里？"
    reopened = Go2RunView(checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
                          index=FileRunIndex(tmp_path / "index"))
    paused = reopened.get("go2-test-clarify", "clarify-session", require_head=True)
    assert paused is not None and paused.status == "needs_clarification"
    assert paused.reply_event_id == first["event_id"]
    second = await go2_graph.run_go2_loop("clarify-session", "另一个请求", context=context,
                                          run_id="go2-test-other-run")
    assert second["result"] == "rejected"
    assert second["error"] == "session_busy"
    resumed = await go2_graph.run_go2_loop("clarify-session", "客厅", context=context,
                                           run_id="go2-test-clarify", resume=True)
    assert resumed["result"] == "ok"
    assert resumed["response"] == "汪，去客厅呀！"
    stored = reopened.get("go2-test-clarify", "clarify-session")
    assert stored is not None and stored.response == resumed["response"]
    assert [fields["reply_kind"] for kind, fields in entry if kind == "agent_reply"] == [
        "clarification", "final"]
