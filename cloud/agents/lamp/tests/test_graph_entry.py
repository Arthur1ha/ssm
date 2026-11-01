"""Lamp Graph run identity and reply survive a fresh process view."""

import time
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.views import load_run
from cloud.agents.lamp import graph as lamp_graph
from cloud.space.execution import ExecutionContext


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)

    def bind_tools(self, _tools, **kwargs):
        assert kwargs["parallel_tool_calls"] is False
        return self

    async def ainvoke(self, _messages):
        return next(self.replies)


def _setup(tmp_path, monkeypatch, replies):
    monkeypatch.setenv("SSM_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    monkeypatch.setenv("SSM_RUN_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(lamp_graph, "decision_messages", lambda *_a, **_k: [
        HumanMessage(content="请调灯")])
    monkeypatch.setattr(lamp_graph, "needs_persona_reply", lambda _run: True)
    monkeypatch.setattr(lamp_graph, "load_personality", lambda: ("灯的人设", {}, 1))
    monkeypatch.setattr(lamp_graph, "get_profile", lambda: {"traits": {}})
    monkeypatch.setattr(lamp_graph, "persona_reply_prompt", lambda *_a, **_k: "reply")
    monkeypatch.setattr(lamp_graph, "valid_persona_reply", lambda *_a, **_k: True)
    monkeypatch.setattr(lamp_graph, "submit_progress", lambda *_a, **_k: None)
    monkeypatch.setattr(lamp_graph, "emit_agent_event", lambda *_a, **_k: None)
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    published = []

    def publish(kind, **fields):
        published.append((kind, fields))
        return {"event_id": fields.get("event_id") or "test-event"}

    monkeypatch.setattr("cloud.api.intents.publish_agent_event", publish)
    agent = SimpleNamespace(_llm=ScriptedModel(replies), _device_online=lambda: True,
                            mark_user_command=lambda _unit: None)
    context = ExecutionContext("lamp-test-session", "admin", True, "main",
                               "user_delegated", int(time.time() + 120))
    return agent, context, published


def test_lamp_reply_checkpoint_and_duplicate_run(tmp_path, monkeypatch):
    agent, context, published = _setup(tmp_path, monkeypatch, [
        AIMessage(content="灯已调整"), AIMessage(content="哼，调好了。")])
    result = lamp_graph.run_lamp_loop(
        agent, "调灯", "esp32_desk_led", context, lambda *_a: None,
        run_id="lamp-run-1")
    assert result["result"] == "ok"
    assert result["reply"] == "哼，调好了。"
    assert result["event_id"].startswith("evt_")
    stored = load_run("lamp-run-1", context.session_id, "esp32_desk_led")
    assert stored.response == result["reply"]
    assert stored.reply_event_id == result["event_id"]
    repeated = lamp_graph.run_lamp_loop(
        agent, "调灯", "esp32_desk_led", context, lambda *_a: None,
        run_id="lamp-run-1")
    assert repeated["result"] == "ok"
    assert repeated["event_id"] == result["event_id"]
    assert [kind for kind, _ in published].count("agent_reply") == 1


def test_lamp_clarification_blocks_new_run_and_resumes(tmp_path, monkeypatch):
    agent, context, published = _setup(tmp_path, monkeypatch, [
        AIMessage(content="", tool_calls=[{
            "name": "ask_user", "args": {"question": "想调成什么感觉？"}, "id": "ask-1"}]),
        AIMessage(content="懂了"), AIMessage(content="哼，暖一点就暖一点。")])
    first = lamp_graph.run_lamp_loop(
        agent, "调灯", "esp32_desk_led", context, lambda *_a: None,
        run_id="lamp-ask-1")
    assert first["result"] == "needs_clarification"
    assert first["question"] == "想调成什么感觉？"
    paused = load_run("lamp-ask-1", context.session_id, "esp32_desk_led")
    assert paused.reply_ready and paused.status == "needs_clarification"
    other = lamp_graph.run_lamp_loop(
        agent, "另一件事", "esp32_desk_led", context, lambda *_a: None,
        run_id="lamp-other")
    assert other["error"] == "session_busy"
    resumed = lamp_graph.run_lamp_loop(
        agent, "暖一点", "esp32_desk_led", context, lambda *_a: None,
        run_id="lamp-ask-1", resume=True)
    assert resumed["result"] == "ok"
    assert resumed["reply"] == "哼，暖一点就暖一点。"
    assert [fields["reply_kind"] for kind, fields in published if kind == "agent_reply"] == [
        "clarification", "final"]
