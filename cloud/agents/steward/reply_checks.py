"""Current-run reply budgets without network calls or saved personalities."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from cloud.agents.common.runner import GraphRun
from cloud.agents.steward.replies import finalize_steward_reply


def execution(draft):
    return GraphRun("current", "session", "你俩玩石头剪刀布", time.time() + 30,
        agent_id="cloud_orchestrator", status="partial", response=draft,
        decision_draft=draft, steps=[
            {"step_id": "start", "tool": "start_activity", "effectful": True,
             "result": {"result": "ok", "activity": {"activity_id": "act_current"}}},
            {"step_id": "handoff", "tool": "send_message", "effectful": True,
             "result": {"result": "rejected", "error": "target_unavailable"}},
        ])


async def test_short_execution_final_uses_original_decision_without_rewriting():
    draft = "活动已启动，邀请尚未送达。"
    run = execution(draft)
    model = SimpleNamespace(ainvoke=AsyncMock())
    await finalize_steward_reply(run, model=model, instructions="简短回应本轮。")
    assert run.response == draft and run.reply_source == "decision_model"
    model.ainvoke.assert_not_awaited()


async def test_long_execution_final_is_recomposed_once_without_old_topic_or_draft():
    old_topic = "上次灯调了紫色，等机器狗连上以后我再补跳舞。"
    run = execution(old_topic + "活动中我不能替它们说话。" * 20)
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(
        content="活动已启动，邀请尚未送达。")))
    await finalize_steward_reply(run, model=model, instructions="只回应本轮目标和真实回执。")
    model.ainvoke.assert_awaited_once()
    messages = model.ainvoke.call_args.args[0]
    assert run.goal in [message.content for message in messages]
    assert not any("紫色" in message.content or "补跳舞" in message.content for message in messages)
    assert any("target_unavailable" in message.content for message in messages)
    assert run.response == "活动已启动，邀请尚未送达。"


async def test_invalid_recomposition_falls_back_without_another_model_attempt():
    run = execution("第一件。第二件。第三件。")
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(
        content="第一件。第二件。第三件。")))
    await finalize_steward_reply(run, model=model, instructions="只回应本轮。")
    model.ainvoke.assert_awaited_once()
    assert run.response == "活动已启动，后续协作结果尚未确认。"
    assert run.reply_source == "runtime"


@pytest.mark.parametrize("with_query", [False, True])
async def test_explanatory_questions_keep_detail_without_execution_budget(with_query):
    draft = "这是详细解释。" * 35
    run = GraphRun("question", "session", "解释一下设备状态为什么不同", time.time() + 30,
        agent_id="cloud_orchestrator", status="ok", decision_draft=draft,
        steps=[{"tool": "read_state", "effectful": False, "result": {"result": "ok"}}]
        if with_query else [])
    model = SimpleNamespace(ainvoke=AsyncMock())
    await finalize_steward_reply(run, model=model, instructions="按用户需要解释。")
    assert run.response == draft
    model.ainvoke.assert_not_awaited()
