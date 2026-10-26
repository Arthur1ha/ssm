import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI

from cloud.api import auth, intents, policy
from cloud.api.devices import router as devices_router
from cloud.api.intents import router as intents_router
from cloud.api.people import router as people_router
from cloud.api.sessions import router as sessions_router
from cloud.space import registry as space_registry
from cloud.space import device_codes


class FakePublishInfo:
    rc = 0


class FakeMqtt:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload, **kwargs):
        self.messages.append((topic, payload, kwargs))
        return FakePublishInfo()


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SSM_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "i" * 32)
    monkeypatch.setenv("SSM_COOKIE_SECURE", "false")
    monkeypatch.setenv("SSM_AUTH_RATE_LIMIT_PER_MIN", "100")
    store = space_registry.SpaceRegistry(tmp_path / "space.json")
    monkeypatch.setattr(space_registry, "_registry", store)
    monkeypatch.setattr(device_codes, "_store", device_codes.DeviceCodeStore(tmp_path / "device_codes.json"))
    monkeypatch.setattr(policy, "_AUDIT_FILE", tmp_path / "audit.jsonl")
    with auth._rate_lock:
        auth._rate_attempts.clear()
    mqtt = FakeMqtt()
    intents.init_mqtt(mqtt)
    yield store, mqtt
    # Cookie-session expiry watchers intentionally outlive requests in
    # production. Tests replace the registry and MQTT client per case, so do
    # not let a prior case's watcher publish cleanup into the next fixture.
    for session_id in tuple(intents._cleanup_tasks):
        intents._cancel_cleanup_task(session_id)
    intents.init_mqtt(None)


@pytest.fixture
def app(api_env):
    app = FastAPI()
    app.include_router(sessions_router)
    app.include_router(intents_router)
    app.include_router(people_router)
    app.include_router(devices_router)
    return app


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as value:
        yield value


async def login_admin(client):
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    return {"X-CSRF-Token": client.cookies.get("ssm_csrf")}
