"""Model output budgets and incomplete decisions must not prevent safe handoff."""

import json
import time

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cloud.agents.common import models
from cloud.agents.common.check_fixtures import JsonReplyModel
from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.common.runner import GraphRun, run_role_graph
from cloud.agents.steward.tools import STEWARD_TOOL_SCHEMAS


@pytest.fixture(autouse=True)
def isolate_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_USER_GOALS_DIR", str(tmp_path / "goals"))
    monkeypatch.setattr("cloud.agents.common.checkpoint.emit_agent_event", lambda *a, **k: None)
    monkeypatch.setattr("cloud.observability.emit_agent_event", lambda *a, **k: None)


@pytest.mark.parametrize("budget", [None, "invalid", "0", "-1", "900", "4096"])
async def test_steward_can_start_game_activity_without_small_implicit_output_cap(
        budget, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.test/v1")
    monkeypatch.delenv("ORCHESTRATOR_MAX_TOKENS", raising=False)
    if budget is not None:
        monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", budget)
    events, requests, executed = [], [], []
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    real_client = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        # A reasoning-capable provider can exhaust a small generation budget
        # before producing any content or tool call.
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        truncated = limit is not None and limit < 4096
        message = {"role": "assistant", "content": ""}
        if not truncated:
            message["tool_calls"] = [{"id": "start-game", "type": "function",
                "function": {"name": "start_activity", "arguments": "{}"}}]
        return httpx.Response(200, json={"id": "test", "object": "chat.completion",
            "created": 1, "model": body["model"], "choices": [{"index": 0,
                "message": message, "finish_reason": "length" if truncated else "tool_calls"}]})

    class LocalClient(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(models.httpx, "AsyncClient", LocalClient)
    current = GraphRun("game-start", "session", "你们两玩石头剪刀布", time.time() + 30,
                       agent_id="cloud_orchestrator")
    async with models.async_model_clients():
        await run_role_graph(current, model=models.get_text_model("steward"),
            tool_schemas=STEWARD_TOOL_SCHEMAS,
            build_messages=lambda run: [HumanMessage(content=run.goal)],
            execute=lambda name, args, step: executed.append((name, args, step)) or {
                "result": "ok", "activity": {"activity_id": "act_test"}},
            checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
            effect_ledger=FileEffectLedger(tmp_path / "effects"))
    if budget == "900":
        assert current.status == "error" and executed == []
    else:
        assert current.status == "ok"
        assert executed == [("start_activity", {}, "start-game")]
    assert len(requests) == 1
    limit = requests[0].get("max_completion_tokens", requests[0].get("max_tokens"))
    assert limit == (int(budget) if budget in {"900", "4096"} else None)


@pytest.mark.parametrize("replies", [
    [AIMessage(content="", response_metadata={"finish_reason": "length"})],
    [AIMessage(content="", tool_calls=[{"name": "start_activity", "args": {}, "id": "partial"}],
               response_metadata={"finish_reason": "length"})],
    [AIMessage(content='{"kind":"final","text":"游戏已启动"}',
               response_metadata={"finish_reason": "length"})],
    [AIMessage(content=""), AIMessage(content="", response_metadata={"finish_reason": "length"})],
])
async def test_truncated_response_is_diagnosed_without_dispatch_or_same_budget_retry(
        replies, tmp_path, monkeypatch):
    events, inputs = [], []
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))

    class Model(JsonReplyModel):
        def bind_tools(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, messages):
            inputs.append(messages)
            return replies[len(inputs) - 1]

    current = GraphRun("truncated", "session", "玩猜拳", time.time() + 30,
                       agent_id="cloud_orchestrator")
    await run_role_graph(current, model=Model(), tool_schemas=STEWARD_TOOL_SCHEMAS,
        build_messages=lambda run: [HumanMessage(content=run.goal)],
        execute=lambda *a: pytest.fail("truncated decision dispatched an action"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    assert current.status == "error" and current.steps == []
    assert current.decision_draft == ""
    assert len(inputs) == len(replies)
    assert any(event == "model.error" and fields["error"] == "ModelOutputTruncated"
               for event, fields in events)


async def test_empty_retry_keeps_finish_reason_and_usage_without_private_reasoning(tmp_path, monkeypatch):
    events, inputs = [], []
    monkeypatch.setattr("cloud.agents.common.runner.emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))

    class Model(JsonReplyModel):
        async def ainvoke(self, messages):
            inputs.append(messages)
            return AIMessage(content="" if len(inputs) == 1 else "暂未启动游戏。",
                additional_kwargs={"reasoning_content": "private reasoning"},
                response_metadata={"finish_reason": "stop", "private_provider_field": "private value"},
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                                "output_token_details": {"reasoning": 8}})

    current = GraphRun("empty-retry", "session", "玩猜拳", time.time() + 30,
                       agent_id="cloud_orchestrator")
    await run_role_graph(current, model=Model(), tool_schemas={},
        build_messages=lambda run: [HumanMessage(content=run.goal)],
        execute=lambda *a: pytest.fail("unexpected action"),
        checkpointer=FileCheckpointSaver(tmp_path / "checkpoints"),
        effect_ledger=FileEffectLedger(tmp_path / "effects"))
    responses = [fields for event, fields in events if event == "model.response"]
    assert len(inputs) == len(responses) == 2 and current.status == "ok"
    for fields in responses:
        assert fields["finish_reason"] == "stop"
        assert fields["usage"] == {"input_tokens": 20, "output_tokens": 10,
                                   "total_tokens": 30, "reasoning_tokens": 8}
    assert responses[-1]["retry"] is True
    assert "private reasoning" not in repr(events) and "private value" not in repr(events)
