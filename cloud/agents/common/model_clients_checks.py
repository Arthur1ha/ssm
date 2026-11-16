"""Model connections must not outlive the asyncio loop that owns them."""

import asyncio
import json

import httpx
import pytest

from cloud.agents.common import models


@pytest.mark.asyncio
async def test_all_roles_share_text_model_and_vision_uses_its_own_setting(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("TEXT_MODEL", " deepseek-text ")
    monkeypatch.setenv("VISION_MODEL", " shared-vision ")
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        requests.append(body["model"])
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1,
            "model": body["model"], "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop"}],
        })

    class LocalClient(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(models.httpx, "AsyncClient", LocalClient)
    async with models.async_model_clients():
        for role in ("steward", "lamp", "go2"):
            assert (await models.get_text_model(role).ainvoke("hello")).content == "ok"
        assert (await models.get_vision_model().ainvoke("inspect")).content == "ok"
    assert requests == ["deepseek-text"] * 3 + ["shared-vision"]


@pytest.mark.parametrize("role", ["lamp", "go2", "steward", "vision"])
def test_successive_event_loops_get_fresh_clients(role, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("TEXT_MODEL", "local-model")
    clients = []
    loops = []
    real_client = httpx.AsyncClient

    def respond(request):
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1,
            "model": "local-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop"}],
        })

    class LocalClient(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)
            clients.append(self)

    monkeypatch.setattr(models.httpx, "AsyncClient", LocalClient)

    async def request():
        loops.append(asyncio.get_running_loop())
        async with models.async_model_clients():
            model = models.get_vision_model() if role == "vision" else models.get_text_model(role)
            assert model.root_async_client._client is clients[-1]
            async with models.async_model_clients():
                again = models.get_text_model("go2")
                assert again.root_async_client._client is clients[-1]
            assert not clients[-1].is_closed
            # Multiple planning/final reply calls may share this run's pool.
            for _ in range(2):
                assert (await model.ainvoke("hello")).content == "ok"

    for _ in range(6):
        asyncio.run(request())
        assert clients[-1].is_closed
    assert len(clients) == 6
    assert len({id(loop) for loop in loops}) == 6
    assert models._async_client.get() is None


@pytest.mark.asyncio
async def test_scope_closes_client_on_failure_and_cancellation():
    for error in (RuntimeError("failed run"), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            async with models.async_model_clients():
                client = models._async_client.get()[1]
                raise error
        assert client.is_closed
        assert models._async_client.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role,schema_name,decision", [
    ("go2", "ActivityDecision", {"action": "social", "reason": "先邀请同伴。"}),
    ("go2", "EventDecision", {"choice": "accept", "reason": "来玩吧。", "teaching_id": None}),
    ("go2", "SocialDecision", {"decision": "skip"}),
    ("lamp", "LampSetup", {"speech": "三种灯色准备好了。", "emotion": "happy", "mapping": {
        "rock": {"r": 255, "g": 0, "b": 0, "brightness": 160},
        "scissors": {"r": 0, "g": 255, "b": 0, "brightness": 160},
        "paper": {"r": 0, "g": 0, "b": 255, "brightness": 160}},
        "color_names": {"rock": "红色", "scissors": "绿色", "paper": "蓝色"}}),
    ("go2", "Go2Setup", {"speech": "我选好动作了。", "emotion": "curious", "mapping": {
        "rock": "Hello", "scissors": "Stretch", "paper": "Content"}}),
    ("go2", "Go2Identification", {"speech": "还没看清灯。", "emotion": "sad",
        "target_visible": False, "target": None}),
    ("go2", "Go2Setup", {"speech": "我选好动作了。", "emotion": {"invalid": True}, "mapping": {
        "rock": "Hello", "scissors": "Stretch", "paper": "Content"}}),
    ("go2", "Go2Setup", {"speech": "我选好动作了。", "emotion": "unsupported", "mapping": {
        "rock": "Hello", "scissors": "Stretch", "paper": "Content"}}),
])
async def test_device_structured_decisions_use_automatic_tools_and_langchain_parser(
        role, schema_name, decision, monkeypatch):
    from cloud.agents.go2.workflows.free_explore import ActivityDecision, EventDecision
    from cloud.agents.go2.workflows.social import SocialDecision
    from cloud.peers.games.rps_decisions import Go2Identification, Go2Setup, LampSetup
    schemas = {schema.__name__: schema for schema in
               (ActivityDecision, EventDecision, SocialDecision, Go2Identification, Go2Setup, LampSetup)}
    schema = schemas[schema_name]
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.test/v1")
    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        # This mirrors the compatible provider that rejects named tool_choice.
        assert "tool_choice" not in body
        assert body["parallel_tool_calls"] is False
        assert len(body["tools"]) == 1
        tool_name = body["tools"][0]["function"]["name"]
        assert tool_name == schema_name
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1,
            "model": "local-model", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "decision", "type": "function", "function": {
                        "name": tool_name, "arguments": json.dumps(decision)}}]},
                "finish_reason": "tool_calls"}],
        })

    class LocalClient(real_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(models.httpx, "AsyncClient", LocalClient)
    async with models.async_model_clients():
        structured = models.get_text_model(role).with_structured_output(
            schema, method="function_calling")
        result = await structured.ainvoke("请用提供的工具作决定，不执行设备动作。")
    assert isinstance(result, schema)
    assert result == schema.model_validate(decision)
    if "emotion" in decision:
        expected = decision["emotion"] if decision["emotion"] in ("happy", "curious", "sad") else "neutral"
        assert result.emotion == expected
        assert result.speech == decision["speech"]
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,stage", [("activity_started", "activity_arbitration"),
                                       ("peer_message", "peer_arbitration")])
async def test_arbitration_errors_remain_safe_and_diagnosable(kind, stage, monkeypatch):
    from cloud.agents.go2.workflows import free_explore
    events = []

    class RejectedModel:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        async def ainvoke(self, _messages):
            raise ValueError("invalid decision")

    monkeypatch.setattr(free_explore, "emit_agent_event",
                        lambda event, **fields: events.append((event, fields)))
    graph = free_explore.build_motivation_graph(
        profile_loader=lambda: {"revision": 1, "traits": {}}, model=RejectedModel())
    result = await graph.ainvoke({"connected": True,
        "activity_request": {"activity_id": "act_test", "content": "一起玩"},
        "pending_event": {"kind": kind, "event_id": "evt_test", "activity_id": "act_test"}})
    assert result["event_decision"] == "defer"
    assert result["next_action"] in {"wait", "none"}
    assert "ValueError" in result["decision_reason"]
    assert events[0][0] == "model.error"
    assert events[0][1]["stage"] == stage
    assert events[0][1]["run_id"] == "evt_test"
    assert events[0][1]["detail"] == "invalid decision"
