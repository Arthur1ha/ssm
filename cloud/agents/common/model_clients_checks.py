"""Model connections must not outlive the asyncio loop that owns them."""

import asyncio

import httpx
import pytest

from cloud.agents.common import models


@pytest.mark.parametrize("role", ["lamp", "go2", "steward", "vision"])
def test_successive_event_loops_get_fresh_clients(role, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("MODEL_LIST", "local-model")
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
