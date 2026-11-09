"""Conversation persists between runs, independently of physical task state."""

import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.context import thread_id_for
from cloud.agents.common.conversation import legacy_dialogue, trim_dialogue
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph


class Model:
    def __init__(self):
        self.inputs = []

    async def ainvoke(self, messages):
        self.inputs.append(messages)
        return AIMessage(content="知道了，你喜欢紫色。")


@pytest.mark.parametrize("agent_id", ["esp32_desk_led", "go2", "cloud_orchestrator"])
@pytest.mark.asyncio
async def test_dialogue_survives_new_run_and_saver_without_crossing_sessions(
        agent_id, tmp_path, monkeypatch):
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    model = Model()
    executed = []

    async def execute(*args):
        executed.append(args)
        return {"result": "ok"}

    async def request(run_id, session_id, goal):
        run = GraphRun(run_id, session_id, goal, time.time() + 30,
                       agent_id=agent_id, origin_kind="user_delegated")
        return await run_role_graph(
            run, model=model, tool_schemas={},
            build_messages=lambda current: [HumanMessage(content=current.goal)],
            execute=execute, checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
            effect_ledger=FileEffectLedger(tmp_path / "effects"))

    await request("first", "same-session", "我最喜欢紫色")
    await request("second", "same-session", "我刚才喜欢什么颜色？")
    contents = [message.content for message in model.inputs[-1]]
    assert contents[-1] == "我刚才喜欢什么颜色？"
    assert "我最喜欢紫色" in contents
    assert "知道了，你喜欢紫色。" in contents
    assert sum(text == "我最喜欢紫色" for text in contents) == 1
    await request("third", "different-session", "我刚才喜欢什么颜色？")
    assert "我最喜欢紫色" not in [message.content for message in model.inputs[-1]]
    assert executed == []
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    head = saver.get_tuple({"configurable": {
        "thread_id": thread_id_for(agent_id, "same-session")}})
    assert len(head.checkpoint["channel_values"]["messages"]) == 4


def test_legacy_history_uses_final_ready_reply_and_keeps_chronological_order():
    from types import SimpleNamespace

    def checkpoint(run_id, goal, **fields):
        return SimpleNamespace(checkpoint={"channel_values": {"run": {
            "run_id": run_id, "goal": goal, "status": "ok", **fields}}})

    saver = SimpleNamespace(list=lambda *_a, **_k: iter([
        checkpoint("current", "当前问题", response="当前回复"),
        checkpoint("second", "第二个问题", response="第二个回复"),
        checkpoint("second", "第二个问题", response="未定稿", reply_ready=False),
        checkpoint("first", "我喜欢紫色", response="记住了"),
        checkpoint("first", "我喜欢紫色", response="重复检查点"),
    ]))
    messages = legacy_dialogue(saver, {}, "current")
    assert [message.content for message in messages] == [
        "我喜欢紫色", "记住了", "第二个问题", "第二个回复"]


def test_dialogue_keeps_recent_complete_pairs_with_a_budget():
    messages = [message for number in range(12) for message in (
        HumanMessage(content=f"question {number}"), AIMessage(content=f"answer {number}"))]
    recent = trim_dialogue(messages)
    assert len(recent) == 16
    assert isinstance(recent[0], HumanMessage)
    assert isinstance(recent[-1], AIMessage)
    assert recent[0].content == "question 4"
    assert recent[-1].content == "answer 11"


@pytest.mark.asyncio
async def test_go2_recovery_preserves_previous_dialogue(tmp_path, monkeypatch):
    import asyncio
    from cloud.agents.common.run_index import FileRunIndex
    from cloud.agents.go2.run_view import Go2RunView

    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event", lambda *_a, **_k: None)
    saver = FileCheckpointSaver(tmp_path / "checkpoints")
    view = Go2RunView(checkpointer=saver, index=FileRunIndex(tmp_path / "index"))
    monkeypatch.setenv("SSM_EFFECT_LEDGER_DIR", str(tmp_path / "effects"))
    first = GraphRun("completed", "session", "我喜欢紫色", time.time() + 30, agent_id="go2")
    view.register(first.run_id, first.session_id)
    await run_role_graph(first, model=Model(), tool_schemas={},
                         build_messages=lambda current: [HumanMessage(content=current.goal)],
                         execute=lambda *_a: None, checkpointer=saver)

    entered = asyncio.Event()

    class CancelledModel:
        async def ainvoke(self, _messages):
            entered.set()
            await asyncio.Event().wait()

    interrupted = GraphRun("interrupted", "session", "新问题", time.time() + 30, agent_id="go2")
    view.register(interrupted.run_id, interrupted.session_id)
    pending = asyncio.create_task(run_role_graph(
        interrupted, model=CancelledModel(), tool_schemas={},
        build_messages=lambda current: [HumanMessage(content=current.goal)],
        execute=lambda *_a: None, checkpointer=saver))
    await asyncio.wait_for(entered.wait(), 3)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    recovered = await view.reconcile_interrupted("session", cancelled=interrupted)
    assert recovered.status == "cancelled"
    head = saver.get_tuple({"configurable": {"thread_id": thread_id_for("go2", "session")}})
    assert [message.content for message in head.checkpoint["channel_values"]["messages"]] == [
        "我喜欢紫色", "知道了，你喜欢紫色。"]
